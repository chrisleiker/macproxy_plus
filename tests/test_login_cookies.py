"""End-to-end login and cookie tests: the real proxy app against a small local site (needs the Docker image)."""
import base64
import http.server
import json
import os
import shutil
import socketserver
import sys
import threading
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.join(os.path.dirname(__file__), "..")

try:
	import flask  # noqa: F401
	import playwright  # noqa: F401
	import PILSVG  # noqa: F401
	HAVE_STACK = True
except ImportError:
	HAVE_STACK = False

SPA = """<!doctype html><html><head><title>spa</title></head><body><div id="root">loading</div>
<script>
async function boot() {
  const me = await (await fetch('/api/me')).json();
  const root = document.getElementById('root');
  if (me.user) { root.innerHTML = '<p id="hello">Hello, ' + me.user + ' (spa)</p>'; return; }
  root.innerHTML = '<form id="f"><input name="user"><input name="pass" type="password"><button type="submit">Log in</button><p id="err"></p></form>';
  document.getElementById('f').addEventListener('submit', async (e) => {
    e.preventDefault();
    const u = e.target.elements.user.value, p = e.target.elements.pass.value;
    const r = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user: u, pass: p})});
    const d = await r.json();
    if (d.ok) boot(); else document.getElementById('err').textContent = 'Invalid login';
  });
}
boot();
</script></body></html>"""

LOGIN = """<!doctype html><html><body><form method="post" action="/login">
<input name="user"><input type="password" name="pass"><input type="hidden" name="csrf" value="tok123">
<input type="checkbox" name="tag" value="a" checked><input type="checkbox" name="tag" value="b" checked>
<button type="submit" name="go" value="1">Sign in</button></form></body></html>"""

SESSIONS = {}  # token -> user


def cookies_of(handler):
	out = {}
	for part in (handler.headers.get("Cookie") or "").split(";"):
		if "=" in part:
			k, v = part.strip().split("=", 1)
			out[k] = v
	return out


class Site(http.server.BaseHTTPRequestHandler):
	def reply(self, body, status=200, ctype="text/html; charset=utf-8", headers=()):
		data = body.encode() if isinstance(body, str) else body
		self.send_response(status)
		self.send_header("Content-Type", ctype)
		self.send_header("Content-Length", str(len(data)))
		for k, v in headers:
			self.send_header(k, v)
		self.end_headers()
		self.wfile.write(data)

	def origin_ok(self):
		origin = self.headers.get("Origin")
		return origin is None or origin == f"http://{self.headers['Host']}"

	def do_GET(self):
		parts = urlparse(self.path)
		path, cookies = parts.path, cookies_of(self)
		if path == "/login":
			self.reply(LOGIN)
		elif path == "/home":
			user = SESSIONS.get(cookies.get("session"))
			if user:
				self.reply(f"<html><body><p id='who'>Welcome {user}</p></body></html>")
			else:
				self.reply("<html><body>please log in</body></html>", 401)
		elif path == "/whoami":
			self.reply(json.dumps(cookies), ctype="application/json")
		elif path == "/app":
			self.reply(SPA)
		elif path == "/api/me":
			self.reply(json.dumps({"user": SESSIONS.get(cookies.get("spa"))}), ctype="application/json")
		elif path == "/jscookie":
			self.reply("<html><body><p>js</p><script>document.cookie='jsmark=1; path=/';</script></body></html>")
		elif path == "/setcookie":
			self.reply("<html><body>set</body></html>", headers=[("Set-Cookie", "plain=1; Path=/; HttpOnly"), ("Set-Cookie", "second=2; Path=/")])
		elif path == "/echoquery":
			self.reply(parts.query, ctype="text/plain")
		elif path == "/secret":
			header = self.headers.get("Authorization", "")
			if header == "Basic " + base64.b64encode(b"bob:pw").decode():
				self.reply("<html><body><p>secret page</p><script>document.body.append(' (js ran)')</script></body></html>")
			else:
				self.reply("denied", 401, headers=[("WWW-Authenticate", 'Basic realm="t"')])
		else:
			self.reply("not found", 404)

	def do_POST(self):
		length = int(self.headers.get("Content-Length") or 0)
		raw = self.rfile.read(length).decode()
		path = urlparse(self.path).path
		if path == "/login":
			if not self.origin_ok():
				return self.reply("bad origin", 403)
			form = parse_qs(raw)
			if form.get("user") == ["alice"] and form.get("pass") == ["wonder"]:
				token = os.urandom(6).hex()
				SESSIONS[token] = "alice"
				self.reply("", 302, headers=[("Location", "/home"), ("Set-Cookie", f"session={token}; Path=/; HttpOnly")])
			else:
				self.reply("<html><body>bad login</body></html>")
		elif path == "/api/login":
			d = json.loads(raw or "{}")
			if d.get("user") == "alice" and d.get("pass") == "wonder":
				token = os.urandom(6).hex()
				SESSIONS[token] = "alice"
				self.reply(json.dumps({"ok": True}), ctype="application/json", headers=[("Set-Cookie", f"spa={token}; Path=/; HttpOnly")])
			else:
				self.reply(json.dumps({"ok": False}), ctype="application/json")
		elif path == "/formecho":
			self.reply(raw, ctype="text/plain")
		else:
			self.reply("not found", 404)

	def log_message(self, *a):
		pass


@unittest.skipUnless(HAVE_STACK, "needs the Docker image (Flask, Playwright and pillow-svg)")
class LoginCookieTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.made_config = False
		if not os.path.exists(os.path.join(ROOT, "config.py")):
			shutil.copy(os.path.join(ROOT, "config.py.example"), os.path.join(ROOT, "config.py"))
			cls.made_config = True
		cls.cwd = os.getcwd()
		os.chdir(ROOT)
		# Another test module may have installed a stub `config`; the proxy needs the real one
		stub = sys.modules.get("config")
		if stub is not None and getattr(stub, "__file__", None) is None:
			sys.modules.pop("config", None)
			sys.modules.pop("utils.html_utils", None)
		import proxy
		cls.proxy = proxy
		cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Site)
		cls.server.daemon_threads = True
		threading.Thread(target=cls.server.serve_forever, daemon=True).start()
		cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
		names = ("RENDER_JAVASCRIPT", "RENDER_TIMEOUT", "RENDER_CACHE_SECONDS", "RENDER_FORMS", "COOKIE_SUPPORT")
		cls.original = {k: getattr(proxy.config, k, None) for k in names}
		proxy.config.RENDER_JAVASCRIPT = True
		proxy.config.RENDER_TIMEOUT = 10
		proxy.config.COOKIE_SUPPORT = True  # cookies and logins are off by default; these tests are about the feature itself

	@classmethod
	def tearDownClass(cls):
		cls.server.shutdown()
		for k, v in cls.original.items():
			setattr(cls.proxy.config, k, v)
		os.chdir(cls.cwd)
		if cls.made_config:
			os.unlink(os.path.join(ROOT, "config.py"))

	def setUp(self):
		self.proxy.cookie_store.clear()
		self.proxy.render_utils._cache.clear()
		self.proxy.render_utils._handoff.clear()
		self.proxy.config.RENDER_CACHE_SECONDS = 0
		self.http = self.proxy.app.test_client()

	def req(self, method, path, client="10.0.0.1", **kw):
		return getattr(self.http, method)(path, base_url=self.base, environ_overrides={"REMOTE_ADDR": client}, **kw)

	def cookies_off(self):
		"""Context manager: the default configuration, with cookie and login support switched off."""
		import contextlib

		@contextlib.contextmanager
		def off():
			self.proxy.config.COOKIE_SUPPORT = False
			try:
				yield
			finally:
				self.proxy.config.COOKIE_SUPPORT = True
		return off()

	def jar_names(self, client="10.0.0.1"):
		return {c.name for c in self.proxy.cookie_store.jar(client)}

	# -- classic form login ----------------------------------------------------

	def login_classic(self, client="10.0.0.1", password="wonder"):
		return self.req("post", "/login", client=client, data={"user": "alice", "pass": password, "csrf": "tok123", "tag": ["a", "b"], "go": "1"})

	def test_classic_login_follows_the_redirect_and_logs_in(self):
		r = self.login_classic()
		self.assertEqual(r.status_code, 200)
		self.assertIn(b"Welcome alice", r.data)  # POST -> 302 -> GET /home worked (not turned into a failed POST)
		self.assertIn("session", self.jar_names())

	def test_session_cookie_is_never_sent_to_the_browser(self):
		r = self.login_classic()
		self.assertIsNone(r.headers.get("Set-Cookie"))
		r = self.req("get", "/setcookie")
		self.assertIsNone(r.headers.get("Set-Cookie"))
		self.assertLessEqual({"plain", "second"}, self.jar_names())  # both Set-Cookie headers were kept, separately

	def test_later_requests_send_the_cookie_upstream(self):
		self.login_classic()
		self.assertIn(b"Welcome alice", self.req("get", "/home").data)
		self.assertIn("session", json.loads(self.req("get", "/whoami").data))

	def test_wrong_password_stays_logged_out(self):
		r = self.login_classic(password="nope")
		self.assertIn(b"bad login", r.data)
		self.assertEqual(self.req("get", "/home").status_code, 401)

	def test_clients_do_not_share_logins(self):
		self.login_classic(client="10.0.0.1")
		self.assertIn(b"Welcome alice", self.req("get", "/home", client="10.0.0.1").data)
		self.assertEqual(self.req("get", "/home", client="10.0.0.2").status_code, 401)
		self.assertEqual(json.loads(self.req("get", "/whoami", client="10.0.0.2").data), {})

	def test_global_cookie_mode_shares_one_jar(self):
		self.proxy.config.COOKIE_CLIENT_KEY = "global"
		try:
			self.login_classic(client="10.0.0.1")
			self.assertIn(b"Welcome alice", self.req("get", "/home", client="10.0.0.2").data)
		finally:
			del self.proxy.config.COOKIE_CLIENT_KEY

	def test_post_goes_upstream_with_a_matching_origin(self):
		# The site answers 403 to a POST whose Origin is not its own, so a successful login proves it matched
		self.assertIn(b"Welcome alice", self.login_classic().data)

	def test_multi_value_form_fields_all_arrive(self):
		r = self.req("post", "/formecho", data={"tag": ["a", "b"], "x": "1"})
		self.assertEqual(sorted(r.data.decode().split("&")), ["tag=a", "tag=b", "x=1"])

	def test_query_string_is_not_duplicated(self):
		self.assertEqual(self.req("get", "/echoquery?a=1&b=2").data, b"a=1&b=2")

	# -- cookies and the renderer ---------------------------------------------------

	def test_cookies_set_by_page_script_are_kept(self):
		self.req("get", "/jscookie")
		self.assertIn("jsmark", self.jar_names())
		self.assertIn("jsmark", json.loads(self.req("get", "/whoami").data))  # and sent on to the site afterwards

	def test_renderer_is_logged_in_after_a_classic_login(self):
		self.login_classic()
		self.assertIn(b"Welcome alice", self.req("get", "/home").data)
		# the JavaScript app sees a different cookie, so it is not logged in until it logs in itself
		self.assertIn(b"Log in", self.req("get", "/app").data)

	def test_rendered_pages_are_not_shared_between_clients(self):
		self.proxy.config.RENDER_CACHE_SECONDS = 300
		self.req("post", "/__mp/form", data=self.spa_login_fields())
		self.assertIn(b"Hello, alice", self.req("get", "/app", client="10.0.0.1").data)
		self.assertIn(b"Log in", self.req("get", "/app", client="10.0.0.2").data)  # B never sees A's cached page

	# -- JavaScript-driven login (form replay) -------------------------------------------

	def spa_login_page(self, client="10.0.0.1"):
		from bs4 import BeautifulSoup
		html = self.req("get", "/app", client=client).data.decode()
		return html, BeautifulSoup(html, "html5lib")

	def spa_login_fields(self, password="wonder"):
		_, soup = self.spa_login_page()
		form = soup.find("form")
		fields = {i["name"]: i.get("value", "") for i in form.find_all("input") if i.get("name")}
		fields.update({"user": "alice", "pass": password})
		return fields

	def test_script_handled_form_is_routed_through_the_proxy(self):
		html, soup = self.spa_login_page()
		form = soup.find("form")
		self.assertEqual((form["action"], form["method"]), ("/__mp/form", "post"))
		hidden = {i["name"]: i["value"] for i in form.find_all("input", type="hidden")}
		self.assertEqual(hidden["__mp_url"], self.base + "/app")
		self.assertEqual(hidden["__mp_form"], "0")
		self.assertNotIn("<script", html)

	def test_javascript_login_through_form_replay(self):
		r = self.req("post", "/__mp/form", data=self.spa_login_fields())
		self.assertEqual(r.status_code, 303)
		self.assertEqual(r.headers["Location"], self.base + "/app")
		self.assertIn("spa", self.jar_names())
		shown = self.req("get", urlparse(r.headers["Location"]).path)
		self.assertIn(b"Hello, alice (spa)", shown.data)
		# the login persists: a later visit renders the logged-in page without replaying anything
		self.assertIn(b"Hello, alice (spa)", self.req("get", "/app").data)

	def test_wrong_password_shows_the_pages_error_message(self):
		r = self.req("post", "/__mp/form", data=self.spa_login_fields(password="nope"))
		self.assertEqual(r.status_code, 303)
		shown = self.req("get", urlparse(r.headers["Location"]).path)
		self.assertIn(b"Invalid login", shown.data)
		self.assertNotIn("spa", self.jar_names())

	def test_form_replay_rejects_a_form_from_another_site(self):
		r = self.req("post", "/__mp/form", data={"__mp_url": "http://evil.example/app", "__mp_form": "0", "user": "x"})
		self.assertEqual(r.status_code, 400)

	def test_form_replay_disabled_without_rendering(self):
		self.proxy.config.RENDER_JAVASCRIPT = False
		try:
			r = self.req("post", "/__mp/form", data={"__mp_url": self.base + "/app", "__mp_form": "0"})
			self.assertEqual(r.status_code, 400)
		finally:
			self.proxy.config.RENDER_JAVASCRIPT = True

	def test_password_is_not_logged(self):
		import io
		import contextlib
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			self.req("post", "/__mp/form", data=self.spa_login_fields(password="s3cretpw"))
			self.login_classic(password="s3cretpw")
		self.assertNotIn("s3cretpw", buf.getvalue())

	# -- HTTP Basic authentication ---------------------------------------------------

	def test_basic_auth_credentials_are_forwarded(self):
		auth = {"Authorization": "Basic " + base64.b64encode(b"bob:pw").decode()}
		r = self.req("get", "/secret", headers=auth)
		self.assertEqual(r.status_code, 200)
		self.assertIn(b"secret page", r.data)
		self.assertIn(b"(js ran)", r.data)  # the renderer was given the credentials too

	def test_basic_auth_challenge_reaches_the_browser(self):
		r = self.req("get", "/secret")
		self.assertEqual(r.status_code, 401)
		self.assertIn("Basic", r.headers.get("WWW-Authenticate", ""))

	# -- switched off (the default) ---------------------------------------------------------

	def test_off_logins_are_not_remembered(self):
		with self.cookies_off():
			r = self.login_classic()
			self.assertIn(b"Welcome alice", r.data)  # cookies still work within one request, so the redirect after the POST works
			self.assertEqual(self.jar_names(), set())  # but nothing is kept
			self.assertEqual(self.req("get", "/home").status_code, 401)
			self.assertEqual(json.loads(self.req("get", "/whoami").data), {})

	def test_off_cookies_set_by_page_script_are_not_kept(self):
		with self.cookies_off():
			self.req("get", "/jscookie")
			self.assertEqual(self.jar_names(), set())
			self.assertEqual(json.loads(self.req("get", "/whoami").data), {})

	def test_off_set_cookie_is_still_never_sent_to_the_browser(self):
		with self.cookies_off():
			self.assertIsNone(self.req("get", "/setcookie").headers.get("Set-Cookie"))

	def test_off_script_forms_are_left_alone_and_replay_is_unavailable(self):
		with self.cookies_off():
			html = self.req("get", "/app").data.decode()
			self.assertNotIn("/__mp/form", html)
			self.assertNotIn("__mp_url", html)
			self.assertEqual(self.req("post", "/__mp/form", data={"__mp_url": self.base + "/app", "__mp_form": "0"}).status_code, 404)

	def test_off_cookie_pages_are_unavailable(self):
		with self.cookies_off():
			self.assertEqual(self.req("get", "/__mp/cookies").status_code, 404)
			self.assertEqual(self.req("post", "/__mp/cookies/clear", data={}).status_code, 404)

	def test_off_the_other_fixes_still_apply(self):
		with self.cookies_off():
			self.assertEqual(self.req("get", "/echoquery?a=1&b=2").data, b"a=1&b=2")
			self.assertEqual(sorted(self.req("post", "/formecho", data={"tag": ["a", "b"]}).data.decode().split("&")), ["tag=a", "tag=b"])
			self.assertEqual(json.loads(self.req("get", "/whoami").data), {})  # JSON is not wrapped in <html>

	def test_off_basic_auth_still_works(self):
		with self.cookies_off():
			auth = {"Authorization": "Basic " + base64.b64encode(b"bob:pw").decode()}
			self.assertIn(b"secret page", self.req("get", "/secret", headers=auth).data)

	def test_off_by_default(self):
		self.assertFalse(self.proxy.cookie_utils.enabled(SimpleNamespace()))

	# -- cookie management page --------------------------------------------------------

	def test_cookie_page_lists_names_but_not_values(self):
		self.login_classic()
		page = self.req("get", "/__mp/cookies").data.decode()
		self.assertIn("session", page)
		token = next(c.value for c in self.proxy.cookie_store.jar("10.0.0.1") if c.name == "session")
		self.assertNotIn(token, page)

	def test_cookies_can_be_forgotten(self):
		self.login_classic()
		self.req("post", "/__mp/cookies/clear", data={})
		self.assertEqual(self.jar_names(), set())
		self.assertEqual(self.req("get", "/home").status_code, 401)

	def test_one_site_can_be_forgotten(self):
		self.req("get", "/setcookie")
		self.req("post", "/__mp/cookies/clear", data={"domain": "127.0.0.1"})
		self.assertEqual(self.jar_names(), set())


if __name__ == "__main__":
	unittest.main()
