"""End-to-end ad blocking: the real proxy and headless browser against a small local site (needs the Docker image)."""
import http.server
import os
import shutil
import socketserver
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

try:
	import flask  # noqa: F401
	import playwright  # noqa: F401
	import PILSVG  # noqa: F401
	HAVE_STACK = True
except ImportError:
	HAVE_STACK = False

NEWS = """<!doctype html><html><head><title>News</title>
<script src="http://ads.test/tracker.js"></script><link rel="stylesheet" href="http://ads.test/ads.css"></head>
<body><h1>Headline</h1><div class="ad">BUY NOW</div><p id="story">Real story text that must survive.</p>
<img id="photo" src="/photo.png"><img id="banner" src="http://ads.test/banner.gif">
<iframe id="adframe" src="http://ads.test/frame.html"></iframe><div id="sponsor-box">sponsored</div>
<script src="/own.js"></script></body></html>"""

AD_PAGE = """<!doctype html><html><head><title>Ad page</title></head><body><p id="content">Page content</p>
<script src="http://localhost:{port}/ad.js"></script></body></html>"""

AD_JS = "document.body.insertAdjacentHTML('beforeend', '<p id=\"injected-ad\">INJECTED AD</p>');"


class Site(http.server.BaseHTTPRequestHandler):
	port = 0

	def do_GET(self):
		path = self.path.split("?")[0]
		routes = {
			"/news": (NEWS, "text/html; charset=utf-8"),
			"/adpage": (AD_PAGE.replace("{port}", str(Site.port)), "text/html; charset=utf-8"),
			"/ad.js": (AD_JS, "application/javascript"),
			"/photo.png": (b"\x89PNG\r\n\x1a\n", "image/png"),
		}
		if path not in routes:
			self.send_response(404)
			self.end_headers()
			return
		body, ctype = routes[path]
		data = body.encode() if isinstance(body, str) else body
		self.send_response(200)
		self.send_header("Content-Type", ctype)
		self.send_header("Content-Length", str(len(data)))
		self.end_headers()
		self.wfile.write(data)

	def log_message(self, *a):
		pass


@unittest.skipUnless(HAVE_STACK, "needs the Docker image (Flask, Playwright and pillow-svg)")
class AdblockProxyTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.made_config = False
		if not os.path.exists(os.path.join(ROOT, "config.py")):
			shutil.copy(os.path.join(ROOT, "config.py.example"), os.path.join(ROOT, "config.py"))
			cls.made_config = True
		cls.cwd = os.getcwd()
		os.chdir(ROOT)
		stub = sys.modules.get("config")
		if stub is not None and getattr(stub, "__file__", None) is None:
			sys.modules.pop("config", None)
			sys.modules.pop("utils.html_utils", None)
		import proxy
		from utils import adblock
		cls.proxy, cls.adblock = proxy, adblock
		cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Site)
		cls.server.daemon_threads = True
		Site.port = cls.server.server_address[1]
		threading.Thread(target=cls.server.serve_forever, daemon=True).start()
		cls.base = f"http://127.0.0.1:{Site.port}"
		cls.original_manager = proxy.adblock_manager
		cls.original = {k: getattr(proxy.config, k, None) for k in ("RENDER_JAVASCRIPT", "RENDER_TIMEOUT", "RENDER_CACHE_SECONDS")}

	@classmethod
	def tearDownClass(cls):
		cls.server.shutdown()
		cls.proxy.adblock_manager = cls.original_manager
		cls.adblock._manager = cls.original_manager
		for k, v in cls.original.items():
			setattr(cls.proxy.config, k, v)
		os.chdir(cls.cwd)
		if cls.made_config:
			os.unlink(os.path.join(ROOT, "config.py"))

	def setUp(self):
		self.http = self.proxy.app.test_client()
		self.proxy.config.RENDER_JAVASCRIPT = False
		self.proxy.config.RENDER_CACHE_SECONDS = 0
		self.proxy.render_utils._cache.clear()
		self.proxy.cookie_store.clear()

	def use(self, enabled=True, rules=("||ads.test^", "##.ad", "###sponsor-box"), allowlist=(), skip=()):
		manager = self.adblock.Manager(SimpleNamespace(ADBLOCK=enabled, ADBLOCK_LISTS=[], ADBLOCK_CUSTOM_RULES=list(rules), ADBLOCK_ALLOWLIST=list(allowlist)))
		manager.skip_domains = set(skip)
		self.proxy.adblock_manager = manager
		self.adblock._manager = manager
		return manager

	def get(self, url_base, path, **headers):
		return self.http.get(path, base_url=url_base, headers=headers, environ_overrides={"REMOTE_ADDR": "10.0.0.1"})

	# -- requests the browser makes -------------------------------------------------------

	def test_ad_image_request_gets_an_empty_gif_without_contacting_the_ad_server(self):
		self.use()
		r = self.get("http://ads.test", "/banner.gif", Referer="http://news.test/", Accept="image/png,image/*;q=0.8,*/*;q=0.5")
		self.assertEqual(r.status_code, 200)  # ads.test does not resolve: a 502 would mean it was fetched
		self.assertEqual(r.headers["Content-Type"], "image/gif")
		self.assertTrue(r.data.startswith(b"GIF89a"))
		self.assertIn("ads.test", r.headers["X-Macproxy-Blocked"])

	def test_script_and_stylesheet_requests_get_empty_stand_ins(self):
		self.use()
		js = self.get("http://ads.test", "/x.js", Referer="http://news.test/", Accept="*/*")
		self.assertEqual((js.status_code, js.data, js.headers["Content-Type"]), (200, b"", "application/javascript"))
		css = self.get("http://ads.test", "/x.css", Referer="http://news.test/", Accept="text/css,*/*;q=0.1")
		self.assertEqual((css.status_code, css.data, css.headers["Content-Type"]), (200, b"", "text/css"))

	def test_a_page_typed_in_directly_is_never_blocked(self):
		self.use()
		r = self.get("http://ads.test", "/", Accept="text/html")
		self.assertEqual(r.status_code, 502)  # it was fetched (and failed to resolve), not answered with a stand-in
		self.assertNotIn("X-Macproxy-Blocked", r.headers)

	def test_unlisted_requests_pass_through(self):
		self.use()
		r = self.get(self.base, "/photo.png", Referer=self.base + "/news", Accept="image/png")
		self.assertEqual(r.status_code, 200)
		self.assertNotIn("X-Macproxy-Blocked", r.headers)

	# A URL with no file extension and Accept: image/* is an image request that reaches the normal fetch, so an
	# unblocked one fails with a 502 (ads.test does not resolve) instead of taking the image-conversion path.
	def test_disabled_blocks_nothing(self):
		self.use(enabled=False)
		r = self.get("http://ads.test", "/banner", Referer="http://news.test/", Accept="image/*")
		self.assertEqual(r.status_code, 502)
		self.assertNotIn("X-Macproxy-Blocked", r.headers)

	def test_allowlisted_domains_and_pages_are_not_blocked(self):
		self.use(allowlist=["ads.test"])
		self.assertEqual(self.get("http://ads.test", "/b", Referer="http://news.test/", Accept="image/*").status_code, 502)
		self.use(allowlist=["news.test"])
		self.assertEqual(self.get("http://ads.test", "/b", Referer="http://www.news.test/", Accept="image/*").status_code, 502)
		self.use()
		self.assertEqual(self.get("http://ads.test", "/b", Referer="http://news.test/", Accept="image/*").status_code, 200)  # control: blocked

	def test_extension_domains_are_never_blocked(self):
		self.use(skip=["ads.test"])
		self.assertEqual(self.get("http://ads.test", "/b", Referer="http://news.test/", Accept="image/*").status_code, 502)

	def test_counts_are_kept(self):
		m = self.use()
		for _ in range(2):
			self.get("http://ads.test", "/b.gif", Referer="http://news.test/", Accept="image/*")
		self.assertEqual(m.blocked["ads.test"], 2)

	# -- pages ---------------------------------------------------------------------------------

	def test_ad_tags_and_elements_are_removed_from_a_page(self):
		self.use()
		html = self.get(self.base, "/news").data.decode()
		self.assertIn("Real story text that must survive.", html)
		self.assertIn('id="photo"', html)
		for gone in ("ads.test", "BUY NOW", "sponsor-box", "adframe", "banner"):
			self.assertNotIn(gone, html, gone)

	def test_page_is_untouched_when_blocking_is_off(self):
		self.use(enabled=False)
		html = self.get(self.base, "/news").data.decode()
		for kept in ("BUY NOW", "sponsor-box", "adframe", "banner"):
			self.assertIn(kept, html, kept)

	def test_cosmetic_filtering_can_be_switched_off(self):
		m = self.use()
		m.cosmetic = False
		html = self.get(self.base, "/news").data.decode()
		self.assertIn("BUY NOW", html)
		self.assertNotIn("ads.test", html)

	# -- the headless browser -------------------------------------------------------------------

	def test_renderer_does_not_load_blocked_third_party_scripts(self):
		self.proxy.config.RENDER_JAVASCRIPT = True
		self.proxy.config.RENDER_TIMEOUT = 10
		self.use(rules=["||localhost^$third-party"])
		blocked_html = self.get(self.base, "/adpage").data.decode()
		self.assertIn("Page content", blocked_html)
		self.assertNotIn("INJECTED AD", blocked_html)

	def test_the_same_page_runs_its_script_when_blocking_is_off(self):
		self.proxy.config.RENDER_JAVASCRIPT = True
		self.proxy.config.RENDER_TIMEOUT = 10
		self.use(enabled=False)
		self.assertIn("INJECTED AD", self.get(self.base, "/adpage").data.decode())

	def test_the_page_being_rendered_is_never_blocked_itself(self):
		self.proxy.config.RENDER_JAVASCRIPT = True
		self.proxy.config.RENDER_TIMEOUT = 10
		self.use(rules=["||127.0.0.1^"])  # would block everything on the test host, including the page itself
		self.assertIn("Page content", self.get(self.base, "/adpage").data.decode())

	# -- status page -----------------------------------------------------------------------------

	def test_status_page_and_url_tester(self):
		self.use()
		self.get("http://ads.test", "/b.gif", Referer="http://news.test/", Accept="image/*")
		page = self.get(self.base, "/__mp/adblock").data.decode()
		self.assertIn("Macproxy ad blocking", page)
		self.assertIn("ads.test", page)  # in the most-blocked table
		verdict = self.get(self.base, "/__mp/adblock?url=http%3A%2F%2Fads.test%2Fx.js&page=news.test").data.decode()
		self.assertIn("would be <b>blocked</b>", verdict)
		self.assertIn("||ads.test^", verdict)
		clean = self.get(self.base, "/__mp/adblock?url=http%3A%2F%2Fnews.test%2Fx.js").data.decode()
		self.assertIn("would <b>not</b> be blocked", clean)

	def test_status_page_when_disabled(self):
		self.use(enabled=False)
		self.assertIn("Ad blocking is off", self.get(self.base, "/__mp/adblock").data.decode())


if __name__ == "__main__":
	unittest.main()
