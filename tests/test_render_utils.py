import http.server
import os
import socketserver
import sys
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import render_utils as R

try:
	import playwright  # noqa: F401
	HAVE_PLAYWRIGHT = True
except ImportError:
	HAVE_PLAYWRIGHT = False

PAGES = {
	"/spa.html": """<!doctype html><html><head><title>spa</title></head><body><div id="root">loading...</div>
<noscript><p id="ns">Please enable JavaScript</p></noscript>
<script>
setTimeout(function () {
  fetch('/data.json').then(function (r) { return r.json(); }).then(function (d) {
    document.getElementById('root').innerHTML = '<h1>' + d.title + '</h1><ul>' + d.items.map(function (i) { return '<li>' + i + '</li>'; }).join('') + '</ul>';
  });
}, 300);
</script></body></html>""",
	"/data.json": '{"title": "Rendered by script", "items": ["alpha", "beta", "gamma"]}',
	"/cssinjs.html": """<!doctype html><html><head><title>css</title><style id="e"></style></head><body><p class="x">styled</p>
<script>
var s = document.getElementById('e');
s.sheet.insertRule('.x { color: rgb(1, 2, 3); display: flex }', 0);
</script></body></html>""",
	"/lazy.html": """<!doctype html><html><head><title>lazy</title></head><body>
<div style="height:3000px">spacer</div><div id="lazy">not loaded</div>
<script>
new IntersectionObserver(function (entries, obs) {
  entries.forEach(function (e) { if (e.isIntersecting) { e.target.textContent = 'lazy content loaded'; obs.disconnect(); } });
}).observe(document.getElementById('lazy'));
</script></body></html>""",
	"/busy.html": """<!doctype html><html><head><title>busy</title></head><body><p id="p">hello</p>
<script>setInterval(function () { fetch('/data.json?' + Math.random()); }, 100);
document.getElementById('p').textContent = 'busy page rendered';</script></body></html>""",
	"/static.html": "<!doctype html><html><head><title>static</title></head><body><p>plain</p></body></html>",
	"/images.html": '<!doctype html><html><body><img src="/pic.png"><script>document.title="t"</script></body></html>',
}
hits = {"/pic.png": 0}


class Handler(http.server.BaseHTTPRequestHandler):
	def do_GET(self):
		path = self.path.split("?")[0]
		if path == "/pic.png":
			hits["/pic.png"] += 1
			body, ctype = b"\x89PNG\r\n\x1a\n", "image/png"
		elif path in PAGES:
			body = PAGES[path].encode()
			ctype = "application/json" if path.endswith(".json") else "text/html; charset=utf-8"
		else:
			self.send_response(404)
			self.end_headers()
			return
		self.send_response(200)
		self.send_header("Content-Type", ctype)
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def log_message(self, *a):
		pass


class DecisionTests(unittest.TestCase):
	def test_disabled_by_default(self):
		self.assertFalse(R.should_render("http://a.test/", SimpleNamespace()))

	def test_enabled_and_skip_list(self):
		cfg = SimpleNamespace(RENDER_JAVASCRIPT=True, RENDER_JAVASCRIPT_SKIP_DOMAINS=["skip.test"])
		self.assertTrue(R.should_render("http://a.test/", cfg))
		self.assertFalse(R.should_render("http://skip.test/x", cfg))
		self.assertFalse(R.should_render("https://www.skip.test:8080/x", cfg))
		self.assertTrue(R.should_render("http://notskip.test/", cfg))

	def test_cache(self):
		R._cache.clear()
		self.assertIsNone(R.cache_get("u", 60))
		R.cache_put("u", "<p>x</p>")
		self.assertEqual(R.cache_get("u", 60), "<p>x</p>")
		self.assertIsNone(R.cache_get("u", -1))  # expired
		for i in range(R.CACHE_MAX_ENTRIES + 5):
			R.cache_put(f"k{i}", "x")
		self.assertLessEqual(len(R._cache), R.CACHE_MAX_ENTRIES)


@unittest.skipUnless(HAVE_PLAYWRIGHT, "playwright (headless Chromium) is not installed")
class RenderTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
		cls.server.daemon_threads = True
		threading.Thread(target=cls.server.serve_forever, daemon=True).start()
		cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
		cls.cfg = SimpleNamespace(RENDER_JAVASCRIPT=True, RENDER_TIMEOUT=10, RENDER_CACHE_SECONDS=0, CSS_VIEWPORT_WIDTH=1024, CSS_VIEWPORT_HEIGHT=768)

	@classmethod
	def tearDownClass(cls):
		cls.server.shutdown()

	def render(self, path, cfg=None):
		html = R.render_page(self.base + path, cfg or self.cfg)
		self.assertIsNotNone(html, f"rendering {path} failed")
		return html

	def test_script_built_content_appears(self):
		html = self.render("/spa.html")
		self.assertIn("Rendered by script", html)
		for item in ("alpha", "beta", "gamma"):
			self.assertIn(f"<li>{item}</li>", html)
		self.assertNotIn("loading...", html)

	def test_noscript_fallback_removed(self):
		self.assertNotIn("Please enable JavaScript", self.render("/spa.html"))

	def test_css_inserted_through_cssom_is_kept(self):
		html = self.render("/cssinjs.html")
		self.assertIn("rgb(1, 2, 3)", html)
		self.assertIn("display: flex", html)

	def test_lazy_content_is_triggered_by_scrolling(self):
		self.assertIn("lazy content loaded", self.render("/lazy.html"))

	def test_page_that_never_goes_idle_still_returns(self):
		started = time.time()
		html = self.render("/busy.html", SimpleNamespace(**{**vars(self.cfg), "RENDER_TIMEOUT": 6}))
		self.assertIn("busy page rendered", html)
		self.assertLess(time.time() - started, 25)

	def test_doctype_kept(self):
		self.assertTrue(self.render("/static.html").startswith("<!DOCTYPE html>"))

	def test_images_are_not_downloaded_by_default(self):
		hits["/pic.png"] = 0
		self.render("/images.html")
		self.assertEqual(hits["/pic.png"], 0)

	def test_images_downloaded_when_not_blocked(self):
		hits["/pic.png"] = 0
		self.render("/images.html", SimpleNamespace(**{**vars(self.cfg), "RENDER_BLOCK_RESOURCES": []}))
		self.assertGreaterEqual(hits["/pic.png"], 1)

	def test_result_is_cached(self):
		cfg = SimpleNamespace(**{**vars(self.cfg), "RENDER_CACHE_SECONDS": 60})
		R._cache.clear()
		first = self.render("/static.html", cfg)
		hits_before = len(R._cache)
		self.assertEqual(self.render("/static.html", cfg), first)
		self.assertEqual(len(R._cache), hits_before)

	def test_unreachable_page_returns_none_not_an_exception(self):
		self.assertIsNone(R.render_page("http://127.0.0.1:1/", SimpleNamespace(**{**vars(self.cfg), "RENDER_TIMEOUT": 4})))

	def test_concurrent_renders(self):
		results = {}

		def go(i):
			results[i] = R.render_page(f"{self.base}/spa.html?n={i}", self.cfg)

		threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
		[t.start() for t in threads]
		[t.join(60) for t in threads]
		self.assertEqual(len(results), 4)
		for html in results.values():
			self.assertIn("Rendered by script", html)


if __name__ == "__main__":
	unittest.main()
