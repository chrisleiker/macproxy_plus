import http.cookiejar
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import cookie_utils as C

FUTURE = int(time.time()) + 3600
PAST = int(time.time()) - 3600


def make_cookie(name="sid", value="abc", domain=".example.com", path="/", expires=FUTURE, secure=True, http_only=True, same_site="Lax"):
	return C.dict_to_cookie({"name": name, "value": value, "domain": domain, "path": path, "expires": expires,
							 "secure": secure, "httpOnly": http_only, "sameSite": same_site})


class ConversionTests(unittest.TestCase):
	def test_dict_round_trip(self):
		cookie = make_cookie()
		d = C.cookie_to_dict(cookie)
		self.assertEqual(d, {"name": "sid", "value": "abc", "domain": ".example.com", "path": "/", "expires": FUTURE,
							 "secure": True, "httpOnly": True, "sameSite": "Lax"})
		again = C.dict_to_cookie(d)
		self.assertEqual(C.cookie_to_dict(again), d)

	def test_session_cookie_has_no_expiry(self):
		cookie = make_cookie(expires=None)
		self.assertIsNone(cookie.expires)
		self.assertTrue(cookie.discard)
		self.assertEqual(C.to_playwright(cookie)["expires"], -1)

	def test_host_only_cookie_keeps_undotted_domain(self):
		cookie = make_cookie(domain="www.example.com")
		self.assertFalse(cookie.domain_specified)
		self.assertEqual(C.to_playwright(cookie)["domain"], "www.example.com")

	def test_to_playwright_shape(self):
		pw = C.to_playwright(make_cookie())
		self.assertEqual(pw, {"name": "sid", "value": "abc", "domain": ".example.com", "path": "/", "expires": float(FUTURE),
							  "httpOnly": True, "secure": True, "sameSite": "Lax"})

	def test_expired_cookie_not_sent_to_playwright(self):
		self.assertIsNone(C.to_playwright(make_cookie(expires=PAST)))

	def test_from_playwright(self):
		cookie = C.from_playwright({"name": "a", "value": "b", "domain": ".x.test", "path": "/p", "expires": -1,
									"httpOnly": False, "secure": False, "sameSite": "None"})
		self.assertEqual((cookie.name, cookie.value, cookie.domain, cookie.path), ("a", "b", ".x.test", "/p"))
		self.assertIsNone(cookie.expires)
		self.assertEqual(C._same_site(cookie), "None")
		self.assertFalse(C._is_http_only(cookie))

	def test_unusable_entries_return_none(self):
		for bad in (None, {}, {"name": "a"}, {"name": "a", "value": "b"}, {"name": "a", "value": "b", "domain": ""}):
			self.assertIsNone(C.dict_to_cookie(bad), bad)


class StoreTests(unittest.TestCase):
	def test_clients_are_isolated(self):
		store = C.CookieStore()
		store.jar("10.0.0.1").set_cookie(make_cookie(name="a", value="1"))
		store.jar("10.0.0.2").set_cookie(make_cookie(name="a", value="2"))
		self.assertEqual([c.value for c in store.jar("10.0.0.1")], ["1"])
		self.assertEqual([c.value for c in store.jar("10.0.0.2")], ["2"])
		self.assertEqual(store.for_playwright("10.0.0.3"), [])

	def test_session_uses_the_clients_jar(self):
		store = C.CookieStore()
		sess = store.session("c1")
		sess.cookies.set("k", "v", domain="x.test")
		self.assertEqual(store.jar("c1").get("k", domain="x.test"), "v")
		self.assertIsNot(store.session("c2").cookies, sess.cookies)

	def test_for_playwright_skips_expired(self):
		store = C.CookieStore()
		jar = store.jar("c")
		jar.set_cookie(make_cookie(name="live"))
		jar.set_cookie(make_cookie(name="dead", expires=PAST))
		self.assertEqual([c["name"] for c in store.for_playwright("c")], ["live"])

	def test_merge_playwright_adds_updates_and_deletes(self):
		store = C.CookieStore()
		store.merge_playwright("c", [{"name": "a", "value": "1", "domain": ".x.test", "path": "/", "expires": -1}])
		store.merge_playwright("c", [{"name": "a", "value": "2", "domain": ".x.test", "path": "/", "expires": -1},
									 {"name": "b", "value": "9", "domain": ".x.test", "path": "/", "expires": FUTURE}])
		self.assertEqual({c.name: c.value for c in store.jar("c")}, {"a": "2", "b": "9"})
		# the site expires cookie b
		store.merge_playwright("c", [{"name": "b", "value": "", "domain": ".x.test", "path": "/", "expires": PAST}])
		self.assertEqual({c.name for c in store.jar("c")}, {"a"})

	def test_least_recently_used_client_is_dropped(self):
		store = C.CookieStore(max_clients=2)
		store.jar("a"), store.jar("b")
		store.jar("a")  # a is now the most recent
		store.jar("c")
		self.assertEqual(sorted(store.clients()), ["a", "c"])

	def test_per_client_cookie_cap(self):
		store = C.CookieStore(max_per_client=3)
		for i in range(6):
			store.merge_playwright("c", [{"name": f"n{i}", "value": "v", "domain": ".x.test", "path": "/", "expires": FUTURE + i}])
		self.assertEqual(len(list(store.jar("c"))), 3)

	def test_clear(self):
		store = C.CookieStore()
		store.jar("a").set_cookie(make_cookie())
		store.clear("a")
		self.assertEqual(list(store.jar("a")), [])


class PersistenceTests(unittest.TestCase):
	def setUp(self):
		self.dir = tempfile.TemporaryDirectory()
		self.path = os.path.join(self.dir.name, "sub", "cookies.json")

	def tearDown(self):
		self.dir.cleanup()

	def test_save_and_reload(self):
		a = C.CookieStore(path=self.path)
		a.jar("10.0.0.1").set_cookie(make_cookie(name="sid", value="secret"))
		a.jar("10.0.0.1").set_cookie(make_cookie(name="gone", expires=PAST))
		self.assertTrue(a.save())
		b = C.CookieStore(path=self.path)
		self.assertEqual({c.name: c.value for c in b.jar("10.0.0.1")}, {"sid": "secret"})

	def test_file_is_private(self):
		a = C.CookieStore(path=self.path)
		a.jar("c").set_cookie(make_cookie())
		a.save()
		self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

	def test_unchanged_state_is_not_rewritten(self):
		a = C.CookieStore(path=self.path)
		a.jar("c").set_cookie(make_cookie())
		self.assertTrue(a.save())
		self.assertFalse(a.save())
		a.jar("c").set_cookie(make_cookie(name="new"))
		self.assertTrue(a.save())

	def test_no_path_means_memory_only(self):
		self.assertFalse(C.CookieStore().save())

	def test_corrupt_file_is_ignored(self):
		os.makedirs(os.path.dirname(self.path))
		with open(self.path, "w") as f:
			f.write("{not json")
		store = C.CookieStore(path=self.path)
		self.assertEqual(store.clients(), [])

	def test_file_with_bad_entries_loads_the_good_ones(self):
		os.makedirs(os.path.dirname(self.path))
		with open(self.path, "w") as f:
			json.dump({"c": [{"name": "ok", "value": "1", "domain": ".x.test", "path": "/", "expires": None}, {"junk": 1}, None]}, f)
		store = C.CookieStore(path=self.path)
		self.assertEqual([c.name for c in store.jar("c")], ["ok"])


class ConfigTests(unittest.TestCase):
	def test_client_key(self):
		self.assertEqual(C.client_key("10.0.0.5"), "10.0.0.5")
		self.assertEqual(C.client_key(None), "unknown")
		self.assertEqual(C.client_key("10.0.0.5", SimpleNamespace(COOKIE_CLIENT_KEY="global")), "global")
		self.assertEqual(C.client_key("10.0.0.5", SimpleNamespace(COOKIE_CLIENT_KEY="IP")), "10.0.0.5")

	def test_store_from_config(self):
		store = C.store_from_config(SimpleNamespace(COOKIE_MAX_CLIENTS=3, COOKIE_MAX_PER_CLIENT=7))
		self.assertEqual((store.max_clients, store.max_per_client, store.path), (3, 7, None))
		store = C.store_from_config(SimpleNamespace())
		self.assertEqual((store.max_clients, store.max_per_client), (C.DEFAULT_MAX_CLIENTS, C.DEFAULT_MAX_PER_CLIENT))


if __name__ == "__main__":
	unittest.main()
