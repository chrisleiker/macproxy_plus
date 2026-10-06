import os
import sys
import unittest
from types import SimpleNamespace

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import js_utils, site_overrides as S


class OverrideTests(unittest.TestCase):
	def test_verge_default(self):
		for url in ("http://www.theverge.com/", "https://theverge.com/tech", "http://cdn.theverge.com/x.js", "http://www.theverge.com:8080/"):
			self.assertTrue(S.strips_javascript(url), url)

	def test_other_sites_untouched(self):
		for url in ("http://example.com/", "http://notheverge.com/", "http://theverge.com.evil.test/", "", None):
			self.assertFalse(S.strips_javascript(url), url)

	def test_config_adds_and_disables(self):
		cfg = SimpleNamespace(SITE_OVERRIDES={"example.com": {"strip_javascript": True}, "theverge.com": {"strip_javascript": False}})
		self.assertTrue(S.strips_javascript("http://www.example.com/", cfg))
		self.assertFalse(S.strips_javascript("http://www.theverge.com/", cfg))

	def test_more_specific_domain_wins(self):
		cfg = SimpleNamespace(SITE_OVERRIDES={"example.com": {"strip_javascript": True}, "ok.example.com": {"strip_javascript": False}})
		self.assertTrue(S.strips_javascript("http://a.example.com/", cfg))
		self.assertFalse(S.strips_javascript("http://ok.example.com/", cfg))

	def test_missing_config_attribute_is_fine(self):
		self.assertFalse(S.strips_javascript("http://example.com/", SimpleNamespace()))


class StripTests(unittest.TestCase):
	def strip(self, html):
		soup = BeautifulSoup(html, "html5lib")
		n = js_utils.strip_javascript(soup)
		return soup, n

	def test_scripts_removed(self):
		soup, n = self.strip('<head><script>x()</script><script src="a.js"></script><script type="module">1</script></head><body>hi</body>')
		self.assertEqual(soup.find_all("script"), [])
		self.assertEqual(n, 3)

	def test_json_data_scripts_removed_too(self):
		soup, _ = self.strip('<script type="application/json" id="__NEXT_DATA__">{"a":1}</script><p>x</p>')
		self.assertEqual(soup.find_all("script"), [])

	def test_event_handlers_removed(self):
		soup, _ = self.strip('<body onload="a()"><a href="/x" onclick="b()" onmouseover="c()" class="k">x</a><input onchange="d()" name="n"></body>')
		a = soup.find("a")
		self.assertEqual(a.attrs, {"href": "/x", "class": ["k"]})
		self.assertEqual(soup.find("input").attrs, {"name": "n"})
		self.assertNotIn("onload", soup.body.attrs)

	def test_non_event_attributes_starting_with_on_are_kept(self):
		soup, _ = self.strip('<p one="1" only="y" onclick="z()">x</p>')
		self.assertEqual(soup.p.attrs, {"one": "1", "only": "y"})

	def test_javascript_urls_defused(self):
		soup, _ = self.strip('<a href="javascript:void(0)">x</a><a href=" JavaScript:go()">y</a><form action="javascript:f()"></form><a href="/ok">z</a>')
		links = soup.find_all("a")
		self.assertEqual([a["href"] for a in links], ["#", "#", "/ok"])
		self.assertEqual(soup.form["action"], "")

	def test_script_preloads_removed_but_stylesheets_kept(self):
		soup, _ = self.strip('<link rel="modulepreload" href="a.js"><link rel="preload" as="script" href="b.js">'
							 '<link rel="preload" as="font" href="f.woff2"><link rel="stylesheet" href="s.css">')
		hrefs = [l["href"] for l in soup.find_all("link")]
		self.assertEqual(hrefs, ["f.woff2", "s.css"])

	def test_content_types(self):
		for ct in ("text/javascript", "application/javascript; charset=utf-8", "APPLICATION/X-JAVASCRIPT"):
			self.assertTrue(js_utils.is_javascript_content_type(ct), ct)
		for ct in ("text/css", "text/html", "application/json", "", None):
			self.assertFalse(js_utils.is_javascript_content_type(ct), ct)


if __name__ == "__main__":
	unittest.main()
