# Standard library imports
import http.cookiejar
import json
import os
import tempfile
import threading
import time

# Third-party imports
import requests
from requests.cookies import RequestsCookieJar

"""
Server-side cookie jars, one per client.

The browser talking to macproxy never stores or sends a site's cookies. Instead the proxy keeps a cookie
jar for each client (identified by its IP address, or one shared jar with COOKIE_CLIENT_KEY = "global").
The same jar is used for the proxy's own requests and for the headless Chromium that renders pages, so a
login made through either one is seen by both.

Settings (config.py, all optional):
  COOKIE_CLIENT_KEY    "ip" (default: a separate jar per client IP) or "global" (one jar for everyone)
  COOKIE_JAR_FILE      path of a JSON file; when set, jars are saved there (permissions 0600) and reloaded on
                       start, so logins survive restarts. Unset = memory only.
  COOKIE_MAX_CLIENTS   how many clients' jars to keep (default 50, least recently used dropped first)
  COOKIE_MAX_PER_CLIENT  cookies kept per client (default 600, oldest dropped first)
"""

DEFAULT_MAX_CLIENTS = 50
DEFAULT_MAX_PER_CLIENT = 600
SAMESITE = {"strict": "Strict", "lax": "Lax", "none": "None"}


# ----------------------------------------------------------------------------------
# Conversions between requests/http.cookiejar cookies, Playwright cookies, and JSON
# ----------------------------------------------------------------------------------

def _is_http_only(cookie):
	return cookie.has_nonstandard_attr("HttpOnly") or cookie.has_nonstandard_attr("httponly")


def _same_site(cookie):
	for key in ("SameSite", "samesite", "Samesite"):
		if cookie.has_nonstandard_attr(key):
			return SAMESITE.get(str(cookie.get_nonstandard_attr(key)).lower())
	return None


def cookie_to_dict(cookie):
	"""A http.cookiejar cookie as a plain dict (the format stored in the JSON file)."""
	return {
		"name": cookie.name,
		"value": cookie.value,
		"domain": cookie.domain,
		"path": cookie.path or "/",
		"expires": cookie.expires,  # unix seconds, or None for a session cookie
		"secure": bool(cookie.secure),
		"httpOnly": _is_http_only(cookie),
		"sameSite": _same_site(cookie),
	}


def dict_to_cookie(d):
	"""Inverse of cookie_to_dict. Returns None for an entry that is not usable."""
	try:
		domain = d["domain"]
		name, value = d["name"], d["value"]
	except (KeyError, TypeError):
		return None
	if not domain or name is None or value is None:
		return None
	expires = d.get("expires")
	rest = {}
	if d.get("httpOnly"):
		rest["HttpOnly"] = None
	if d.get("sameSite"):
		rest["SameSite"] = d["sameSite"]
	dotted = domain.startswith(".")
	return http.cookiejar.Cookie(
		version=0, name=str(name), value=str(value), port=None, port_specified=False,
		domain=domain, domain_specified=dotted, domain_initial_dot=dotted,
		path=d.get("path") or "/", path_specified=True, secure=bool(d.get("secure")),
		expires=int(expires) if expires else None, discard=not expires,
		comment=None, comment_url=None, rest=rest, rfc2109=False,
	)


def to_playwright(cookie, now=None):
	"""A cookie in the shape Playwright's add_cookies() expects, or None if it has expired."""
	now = now or time.time()
	if cookie.expires and cookie.expires <= now:
		return None
	out = {
		"name": cookie.name,
		"value": cookie.value,
		"domain": cookie.domain,
		"path": cookie.path or "/",
		"expires": float(cookie.expires) if cookie.expires else -1,
		"httpOnly": _is_http_only(cookie),
		"secure": bool(cookie.secure),
	}
	same_site = _same_site(cookie)
	if same_site:
		out["sameSite"] = same_site
	return out


def from_playwright(d):
	"""A Playwright cookie dict (from context.cookies()) as a http.cookiejar cookie."""
	expires = d.get("expires")
	return dict_to_cookie({
		"name": d.get("name"), "value": d.get("value"), "domain": d.get("domain"), "path": d.get("path"),
		"expires": None if expires is None or expires < 0 else expires,
		"secure": d.get("secure"), "httpOnly": d.get("httpOnly"), "sameSite": d.get("sameSite"),
	})


# ----------------------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------------------

class CookieStore:
	def __init__(self, path=None, max_clients=DEFAULT_MAX_CLIENTS, max_per_client=DEFAULT_MAX_PER_CLIENT):
		self.path = path
		self.max_clients = max_clients
		self.max_per_client = max_per_client
		self._jars = {}       # client key -> RequestsCookieJar (insertion order = least recently used first)
		self._lock = threading.RLock()
		self._saved = None    # last serialized state, to skip redundant writes
		if path:
			self.load()

	# -- access ---------------------------------------------------------------

	def jar(self, client):
		"""The cookie jar for `client`, created on first use."""
		with self._lock:
			jar = self._jars.pop(client, None)
			if jar is None:
				jar = RequestsCookieJar()
			self._jars[client] = jar  # most recently used goes last
			while len(self._jars) > self.max_clients:
				self._jars.pop(next(iter(self._jars)))
			return jar

	def session(self, client):
		"""A requests.Session that reads and writes this client's cookies."""
		sess = requests.Session()
		sess.cookies = self.jar(client)
		return sess

	def clients(self):
		with self._lock:
			return list(self._jars)

	def clear(self, client=None):
		with self._lock:
			if client is None:
				self._jars.clear()
			else:
				self._jars.pop(client, None)

	# -- Playwright -----------------------------------------------------------

	def for_playwright(self, client):
		"""All of this client's unexpired cookies, ready for context.add_cookies()."""
		now = time.time()
		jar = self.jar(client)
		out = []
		with self._lock:
			for cookie in list(jar):
				converted = to_playwright(cookie, now)
				if converted:
					out.append(converted)
		return out

	def merge_playwright(self, client, cookies):
		"""Store cookies read back from the headless browser (context.cookies()). Returns how many were stored."""
		jar = self.jar(client)
		stored = 0
		with self._lock:
			for d in cookies or []:
				cookie = from_playwright(d)
				if cookie is None:
					continue
				if cookie.expires and cookie.expires <= time.time():
					# The site deleted this cookie: remove it from the jar too
					try:
						jar.clear(cookie.domain, cookie.path, cookie.name)
					except KeyError:
						pass
					continue
				jar.set_cookie(cookie)
				stored += 1
			self._trim(jar)
		return stored

	def _trim(self, jar):
		cookies = list(jar)
		extra = len(cookies) - self.max_per_client
		if extra > 0:
			# Drop session cookies with no expiry information last; oldest expiry first
			cookies.sort(key=lambda c: (c.expires is None, c.expires or 0))
			for cookie in cookies[:extra]:
				try:
					jar.clear(cookie.domain, cookie.path, cookie.name)
				except KeyError:
					pass

	# -- persistence ----------------------------------------------------------

	def _serialize(self):
		now = time.time()
		state = {}
		for client, jar in self._jars.items():
			state[client] = [cookie_to_dict(c) for c in jar if not (c.expires and c.expires <= now)]
		return json.dumps(state, sort_keys=True)

	def save(self, force=False):
		"""Write all jars to the file if anything changed since the last write. Returns True if it wrote."""
		if not self.path:
			return False
		with self._lock:
			data = self._serialize()
			if data == self._saved and not force:
				return False
			directory = os.path.dirname(os.path.abspath(self.path)) or "."
			try:
				os.makedirs(directory, exist_ok=True)
				fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cookies-", suffix=".tmp")
				try:
					with os.fdopen(fd, "w") as f:
						f.write(data)
					os.chmod(tmp, 0o600)
					os.replace(tmp, self.path)
				finally:
					if os.path.exists(tmp):
						os.unlink(tmp)
			except OSError as e:
				print(f"Cookies: could not save {self.path}: {e}")
				return False
			self._saved = data
			return True

	def load(self):
		if not self.path or not os.path.exists(self.path):
			return 0
		count = 0
		try:
			with open(self.path) as f:
				state = json.load(f)
		except (OSError, ValueError) as e:
			print(f"Cookies: could not read {self.path}: {e}")
			return 0
		with self._lock:
			for client, entries in (state or {}).items():
				jar = RequestsCookieJar()
				for d in entries or []:
					cookie = dict_to_cookie(d)
					if cookie is not None and not (cookie.expires and cookie.expires <= time.time()):
						jar.set_cookie(cookie)
						count += 1
				self._jars[client] = jar
			self._saved = self._serialize()
		print(f"Cookies: loaded {count} cookies for {len(self._jars)} client(s) from {self.path}")
		return count


# ----------------------------------------------------------------------------------
# Configuration helpers
# ----------------------------------------------------------------------------------

def client_key(remote_addr, config=None):
	"""Which cookie jar a request belongs to."""
	if str(getattr(config, "COOKIE_CLIENT_KEY", "ip") or "ip").lower() == "global":
		return "global"
	return remote_addr or "unknown"


def store_from_config(config):
	return CookieStore(
		path=getattr(config, "COOKIE_JAR_FILE", None) or None,
		max_clients=int(getattr(config, "COOKIE_MAX_CLIENTS", None) or DEFAULT_MAX_CLIENTS),
		max_per_client=int(getattr(config, "COOKIE_MAX_PER_CLIENT", None) or DEFAULT_MAX_PER_CLIENT),
	)
