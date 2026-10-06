import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import adblock as A


def engine(*lines):
	e = A.Engine()
	e.add_text("\n".join(lines))
	return e


def blocked(e, url, rtype="script", page="news.test"):
	return e.match(url, rtype, page) is not None


class HelperTests(unittest.TestCase):
	def test_registrable_domain(self):
		self.assertEqual(A.registrable_domain("a.b.example.com"), "example.com")
		self.assertEqual(A.registrable_domain("www.bbc.co.uk"), "bbc.co.uk")
		self.assertEqual(A.registrable_domain("127.0.0.1"), "127.0.0.1")
		self.assertEqual(A.registrable_domain("localhost"), "localhost")
		self.assertEqual(A.registrable_domain(""), "")

	def test_third_party(self):
		self.assertTrue(A.is_third_party("ads.other.com", "news.test"))
		self.assertFalse(A.is_third_party("cdn.news.test", "www.news.test"))
		self.assertFalse(A.is_third_party("x.com", None))

	def test_host_and_parents(self):
		self.assertEqual(A.host_and_parents("a.b.example.com"), ["a.b.example.com", "b.example.com", "example.com", "com"])

	def test_guess_request_type(self):
		g = A.guess_request_type
		self.assertEqual(g("http://x/a.png"), "image")
		self.assertEqual(g("http://x/a", "image/webp,*/*"), "image")
		self.assertEqual(g("http://x/a.css"), "stylesheet")
		self.assertEqual(g("http://x/a.js?v=1"), "script")
		self.assertEqual(g("http://x/page", "text/html", has_referer=True), "subdocument")
		self.assertEqual(g("http://x/page", "text/html", has_referer=False), "document")
		self.assertEqual(g("http://x/f.woff2"), "font")

	def test_pattern_to_regex(self):
		import re
		r = lambda p, u: re.search(A.pattern_to_regex(p), u) is not None
		self.assertTrue(r("/ads/*.gif", "http://x.com/ads/a/b.gif"))
		self.assertTrue(r("banner^", "http://x.com/banner?x=1"))
		self.assertTrue(r("banner^", "http://x.com/banner"))
		self.assertFalse(r("banner^", "http://x.com/banners"))
		self.assertTrue(r("|http://x.", "http://x.com/"))
		self.assertFalse(r("|http://x.", "https://x.com/"))
		self.assertTrue(r(".swf|", "http://x.com/a.swf"))
		self.assertFalse(r(".swf|", "http://x.com/a.swf?1"))


class DomainRuleTests(unittest.TestCase):
	def test_domain_anchor_blocks_host_and_subdomains_only(self):
		e = engine("||ads.example.com^")
		self.assertTrue(blocked(e, "http://ads.example.com/x.js"))
		self.assertTrue(blocked(e, "https://sub.ads.example.com/x.js"))
		self.assertFalse(blocked(e, "http://example.com/x.js"))
		self.assertFalse(blocked(e, "http://notads.example.com/x.js"))
		self.assertFalse(blocked(e, "http://ads.example.com.evil.test/x.js"))
		self.assertFalse(blocked(e, "http://evil.test/?u=ads.example.com"))

	def test_domain_with_path(self):
		e = engine("||example.com/ads/")
		self.assertTrue(blocked(e, "http://example.com/ads/banner.gif"))
		self.assertTrue(blocked(e, "http://www.example.com/ads/banner.gif"))
		self.assertFalse(blocked(e, "http://example.com/news/banner.gif"))

	def test_domain_with_separator_and_path(self):
		e = engine("||example.com^ads^")
		self.assertTrue(blocked(e, "http://example.com/ads/x"))
		self.assertFalse(blocked(e, "http://example.com/adsense/x"))

	def test_wildcard_in_host(self):
		e = engine("||ad*.example.com^")
		self.assertTrue(blocked(e, "http://ad7.example.com/x"))
		self.assertFalse(blocked(e, "http://news.example.com/x"))

	def test_hosts_file_format(self):
		e = engine("# comment", "127.0.0.1 localhost", "0.0.0.0 tracker.test", "0.0.0.0 a.test b.test  # two", "::1 ip6-localhost")
		self.assertTrue(blocked(e, "http://tracker.test/p.gif"))
		self.assertTrue(blocked(e, "http://a.test/"))
		self.assertTrue(blocked(e, "http://b.test/"))
		self.assertFalse(blocked(e, "http://localhost/"))

	def test_comments_and_headers_are_ignored(self):
		e = engine("[Adblock Plus 2.0]", "! Title: x", "", "   ", "# hosts comment")
		self.assertEqual(e.size["network_rules"], 0)


class GeneralRuleTests(unittest.TestCase):
	def test_path_pattern_anywhere(self):
		e = engine("/banners/*")
		self.assertTrue(blocked(e, "http://x.com/banners/a.gif", "image"))
		self.assertFalse(blocked(e, "http://x.com/banner/a.gif", "image"))

	def test_start_anchor(self):
		e = engine("|http://ads.")
		self.assertTrue(blocked(e, "http://ads.x.com/"))
		self.assertFalse(blocked(e, "https://ads.x.com/"))

	def test_substring_rule_without_a_safe_token(self):
		e = engine("advert")
		self.assertTrue(blocked(e, "http://x.com/adverts/1.js"))
		self.assertTrue(blocked(e, "http://x.com/1.js?advertiser=1"))
		self.assertFalse(blocked(e, "http://x.com/ad.js"))

	def test_regex_rule(self):
		e = engine(r"/\/ad[0-9]+\.js/")
		self.assertTrue(blocked(e, "http://x.com/ad42.js"))
		self.assertFalse(blocked(e, "http://x.com/adx.js"))

	def test_token_index_does_not_miss_matches(self):
		e = engine("-banner-ad.", "/pagead/*", "&adtype=")
		self.assertTrue(blocked(e, "http://x.com/a-banner-ad.png", "image"))
		self.assertTrue(blocked(e, "http://x.com/pagead/show"))
		self.assertTrue(blocked(e, "http://x.com/s?a=1&adtype=2"))

	def test_case_insensitive_by_default(self):
		self.assertTrue(blocked(engine("/Banner/"), "http://x.com/banner/a"))
		self.assertFalse(blocked(engine("/Banner/$match-case"), "http://x.com/banner/a"))
		self.assertTrue(blocked(engine("/Banner/$match-case"), "http://x.com/Banner/a"))


class OptionTests(unittest.TestCase):
	def test_type_options(self):
		e = engine("||cdn.test^$script")
		self.assertTrue(blocked(e, "http://cdn.test/a.js", "script"))
		self.assertFalse(blocked(e, "http://cdn.test/a.png", "image"))

	def test_negated_types(self):
		e = engine("||cdn.test^$~image")
		self.assertTrue(blocked(e, "http://cdn.test/a.js", "script"))
		self.assertFalse(blocked(e, "http://cdn.test/a.png", "image"))

	def test_multiple_types(self):
		e = engine("||cdn.test^$image,stylesheet")
		self.assertTrue(blocked(e, "http://cdn.test/a", "image"))
		self.assertTrue(blocked(e, "http://cdn.test/a", "stylesheet"))
		self.assertFalse(blocked(e, "http://cdn.test/a", "script"))

	def test_third_party(self):
		e = engine("||cdn.test^$third-party")
		self.assertTrue(blocked(e, "http://cdn.test/a.js", "script", page="news.test"))
		self.assertFalse(blocked(e, "http://cdn.test/a.js", "script", page="www.cdn.test"))
		self.assertFalse(blocked(e, "http://cdn.test/a.js", "script", page=None))  # unknown first party: stay safe

	def test_first_party_only(self):
		e = engine("||cdn.test^$~third-party")
		self.assertTrue(blocked(e, "http://cdn.test/a.js", "script", page="cdn.test"))
		self.assertFalse(blocked(e, "http://cdn.test/a.js", "script", page="news.test"))

	def test_domain_option(self):
		e = engine("/promo.$domain=news.test|~live.news.test")
		self.assertTrue(blocked(e, "http://x.com/promo.js", page="news.test"))
		self.assertTrue(blocked(e, "http://x.com/promo.js", page="www.news.test"))
		self.assertFalse(blocked(e, "http://x.com/promo.js", page="live.news.test"))
		self.assertFalse(blocked(e, "http://x.com/promo.js", page="other.test"))

	def test_aliases(self):
		e = engine("||cdn.test^$xhr", "||frame.test^$frame,3p")
		self.assertTrue(blocked(e, "http://cdn.test/api", "xmlhttprequest"))
		self.assertTrue(blocked(e, "http://frame.test/", "subdocument", page="news.test"))

	def test_unsupported_options_skip_the_rule(self):
		e = engine("||a.test^$redirect=noopjs", "||b.test^$csp=script-src 'none'", "||c.test^$removeparam=utm", "||d.test^$popup", "||e.test^$badfilter")
		for host in "abcde":
			self.assertFalse(blocked(e, f"http://{host}.test/x"), host)

	def test_parse_options(self):
		self.assertIsNone(A.parse_options("redirect=x"))
		self.assertIsNone(A.parse_options("script,csp=x"))
		self.assertEqual(A.parse_options("script,third-party")["types"], {"script"})
		self.assertIs(A.parse_options("~third-party")["third"], False)
		self.assertIs(A.parse_options("3p")["third"], True)

	def test_slashes_make_a_regex_rule_even_with_a_dollar_sign(self):
		self.assertTrue(blocked(engine("/ad[0-9]$/"), "http://x.com/ad7"))


class ExceptionTests(unittest.TestCase):
	def test_exception_wins(self):
		e = engine("||ads.test^", "@@||ads.test/allowed/")
		self.assertTrue(blocked(e, "http://ads.test/banner.js"))
		self.assertFalse(blocked(e, "http://ads.test/allowed/a.js"))

	def test_exception_with_domain_option(self):
		e = engine("||ads.test^", "@@||ads.test^$domain=friend.test")
		self.assertFalse(blocked(e, "http://ads.test/x", page="friend.test"))
		self.assertTrue(blocked(e, "http://ads.test/x", page="news.test"))

	def test_exception_for_general_rules(self):
		e = engine("/ads/*", "@@/ads/ok.js")
		self.assertTrue(blocked(e, "http://x.com/ads/bad.js"))
		self.assertFalse(blocked(e, "http://x.com/ads/ok.js"))

	def test_document_exception_switches_everything_off_for_a_site(self):
		e = engine("||ads.test^", "@@||friend.test^$document")
		self.assertTrue(blocked(e, "http://ads.test/x", page="news.test"))
		self.assertFalse(blocked(e, "http://ads.test/x", page="friend.test"))
		self.assertEqual(e.selectors_for("friend.test"), [])


class ElementHidingTests(unittest.TestCase):
	def test_generic_and_domain_selectors(self):
		e = engine("##.ad", "news.test##.promo", "other.test##.nope")
		self.assertEqual(e.selectors_for("news.test"), [".ad", ".promo"])
		self.assertEqual(e.selectors_for("www.news.test"), [".ad", ".promo"])
		self.assertEqual(e.selectors_for("zzz.test"), [".ad"])

	def test_exceptions(self):
		e = engine("##.ad", "##.sponsor", "news.test#@#.ad")
		self.assertEqual(e.selectors_for("news.test"), [".sponsor"])
		self.assertEqual(e.selectors_for("zzz.test"), [".ad", ".sponsor"])

	def test_excluded_domains(self):
		e = engine("~news.test##.ad")
		self.assertEqual(e.selectors_for("news.test"), [])
		self.assertEqual(e.selectors_for("zzz.test"), [".ad"])

	def test_procedural_selectors_are_skipped(self):
		e = engine("##.a:has-text(Sponsored)", "##div:-abp-contains(Ad)", "##.b:xpath(//x)", "example.com#?#.c", "example.com##+js(abort)", "##.plain:has(.x)")
		self.assertEqual(e.selectors_for("example.com"), [".plain:has(.x)"])

	def test_generichide(self):
		e = engine("##.ad", "site.test##.own", "@@||site.test^$generichide")
		self.assertEqual(e.selectors_for("site.test"), [".own"])


class PageFilterTests(unittest.TestCase):
	def filter(self, html, *rules, url="http://news.test/story", cosmetic=True, allowlist=()):
		soup = BeautifulSoup(html, "html5lib")
		result = A.apply_to_soup(soup, url, engine(*rules), cosmetic=cosmetic, allowlist=allowlist)
		return soup, result

	def test_tags_loading_blocked_urls_are_removed(self):
		soup, r = self.filter('<body><script src="http://ads.test/a.js"></script><iframe src="//ads.test/f"></iframe>'
							  '<img src="http://ads.test/p.gif"><img src="/own.png"><link rel="stylesheet" href="http://ads.test/s.css">'
							  '<script src="/own.js"></script></body>', "||ads.test^")
		self.assertEqual(r["requests"], 4)
		self.assertEqual(len(soup.find_all("script")), 1)
		self.assertEqual(len(soup.find_all("img")), 1)
		self.assertEqual(soup.find_all("iframe"), [])
		self.assertEqual(soup.find_all("link"), [])
		self.assertEqual(set(r["hosts"]), {"ads.test"})

	def test_relative_urls_resolve_against_the_page(self):
		soup, r = self.filter('<body><img src="/ads/banner.gif"><img src="/pic.gif"></body>', "||news.test/ads/")
		self.assertEqual([i["src"] for i in soup.find_all("img")], ["/pic.gif"])

	def test_type_matters(self):
		soup, _ = self.filter('<body><img src="http://cdn.test/a"><script src="http://cdn.test/b"></script></body>', "||cdn.test^$script")
		self.assertEqual(len(soup.find_all("img")), 1)
		self.assertEqual(soup.find_all("script"), [])

	def test_data_urls_and_empty_srcs_are_left_alone(self):
		soup, r = self.filter('<body><img src="data:image/gif;base64,AAAA"><img src=""><img></body>', "||data^", "/image/")
		self.assertEqual(r["requests"], 0)

	def test_cosmetic_rules_remove_elements(self):
		soup, r = self.filter('<body><div class="ad">buy</div><p>story</p><div id="sponsor">s</div><ul><li class="promo">p</li></ul></body>',
							  "##.ad", "news.test###sponsor", "other.test##p", "##li.promo")
		self.assertEqual(r["cosmetic"], 3)
		self.assertEqual(soup.body.get_text(strip=True), "story")

	def test_attribute_selectors(self):
		soup, _ = self.filter('<body><a href="http://x.test/aff?id=1">x</a><a href="/ok">ok</a><div id="div-gpt-ad-1">g</div></body>',
							  '##a[href*="/aff?"]', '##[id^="div-gpt-ad"]')
		self.assertEqual([a.get_text() for a in soup.find_all("a")], ["ok"])
		self.assertIsNone(soup.find(id="div-gpt-ad-1"))

	def test_cosmetic_can_be_switched_off(self):
		soup, r = self.filter('<body><div class="ad">buy</div></body>', "##.ad", cosmetic=False)
		self.assertEqual(r["cosmetic"], 0)
		self.assertIsNotNone(soup.find(class_="ad"))

	def test_allowlisted_pages_are_untouched(self):
		html = '<body><div class="ad">buy</div><img src="http://ads.test/p.gif"></body>'
		soup, r = self.filter(html, "##.ad", "||ads.test^", allowlist=["news.test"])
		self.assertEqual((r["requests"], r["cosmetic"]), (0, 0))
		soup, r = self.filter(html, "##.ad", "||ads.test^", url="http://www.news.test/x", allowlist=["news.test"])
		self.assertEqual((r["requests"], r["cosmetic"]), (0, 0))

	def test_a_bad_generic_rule_cannot_blank_the_page(self):
		text = "word " * 2000
		soup, r = self.filter(f'<body><div class="content">{text}</div><div class="ad">buy</div></body>', "##.content", "##.ad")
		self.assertIsNotNone(soup.find(class_="content"))  # too much of the page to remove
		self.assertIsNone(soup.find(class_="ad"))

	def test_nested_matches_do_not_crash(self):
		soup, r = self.filter('<body><div class="ad"><div class="ad"><span class="ad">x</span></div></div><p>keep</p></body>', "##.ad")
		self.assertEqual(soup.body.get_text(strip=True), "keep")

	def test_invalid_selectors_are_skipped(self):
		soup, r = self.filter('<body><div class="ad">x</div></body>', "##div[", "##:::bad", "##.ad")
		self.assertIsNone(soup.find(class_="ad"))

	def test_never_removes_html_head_or_body(self):
		soup, _ = self.filter("<body><p>x</p></body>", "##body", "##html", "##head")
		self.assertIsNotNone(soup.body)


class FastSelectorTests(unittest.TestCase):
	"""The fast matcher and the attribute index must agree with soupsieve exactly."""

	HTML = """<body id="top" class="page home">
	<div id="ad" class="ad banner" data-x="Hello-World" title="Big Sale">a</div>
	<div class="ad-box sponsored" data-x="hello">b</div>
	<p class="ad" lang="en-US">c</p>
	<a href="http://x.test/aff?id=1" rel="nofollow sponsored" class="link">d</a>
	<a href="/ok" class="link">e</a>
	<iframe src="http://doubleclick.net/f" id="google_ads_iframe_1" width="300"></iframe>
	<div id="div-gpt-ad-123" data-slot=""></div>
	<span class="">f</span><span data-x="a b c">g</span><img alt="x" src="a.png"><INPUT type="text" name="q" value="AdFoo"></body>"""

	SELECTORS = [
		"div", "p", "*", "#ad", ".ad", "div.ad", "p.ad", ".ad.banner", "div#ad.ad", ".nothere", "a.link", "[id]", "[class]", "[data-x]",
		'[data-x="hello"]', '[data-x="Hello-World"]', '[data-x="HELLO" i]', '[data-x^="hello"]', '[data-x$="world"]', '[data-x*="llo"]',
		'[data-x~="b"]', '[data-x|="Hello"]', '[lang|="en"]', '[rel~="sponsored"]', 'a[href*="/aff?"]', 'a[href^="http://x.test"]',
		'a[href$="/ok"]', 'iframe[src*="doubleclick.net"]', '[id^="google_ads"]', '[id^="div-gpt-ad"]', '[data-slot=""]', '[data-slot]',
		'[data-x^=""]', "div[class*='ad']", 'input[value*="ad" i]', "input[type=text]", 'input[name="q"][type="text"]', "[title]", 'div[title="Big Sale"]',
		'[width="300"]', "body", "body.page", "#top.home", "span", "span[class]", "span[data-x]", "img[alt]", "[alt=x]",
	]

	def test_fast_matcher_agrees_with_soupsieve(self):
		import soupsieve
		soup = BeautifulSoup(self.HTML, "html5lib")
		elements = soup.find_all(True)
		for selector in self.SELECTORS:
			fast = A.compile_selector(selector)
			for el in elements:
				self.assertEqual(bool(fast(el)), soupsieve.match(selector, el), f"{selector} on <{el.name} {el.attrs}>")

	def test_index_finds_every_element_a_selector_can_match(self):
		import soupsieve
		soup = BeautifulSoup(self.HTML, "html5lib")
		elements = soup.find_all(True)
		index = A.HideIndex(elements)
		for selector in self.SELECTORS:
			truth = {id(e) for e in elements if soupsieve.match(selector, e)}
			found = {id(e) for e in index.candidates(selector) if soupsieve.match(selector, e)}
			self.assertEqual(found, truth, selector)

	def test_attribute_selectors_do_not_scan_every_element(self):
		soup = BeautifulSoup(self.HTML, "html5lib")
		index = A.HideIndex(soup.find_all(True))
		self.assertLess(len(index.candidates('[data-slot=""]')), len(index.all) // 2)
		self.assertEqual(len(index.candidates("[width]")), 1)

	def test_complex_selectors_use_soupsieve(self):
		soup = BeautifulSoup(self.HTML, "html5lib")
		fast = A.compile_selector("body > div.ad")
		self.assertEqual([e.get("id") for e in soup.find_all(True) if fast(e)], ["ad"])
		fast = A.compile_selector("a.link:not([rel])")
		self.assertEqual([e.get("href") for e in soup.find_all(True) if fast(e)], ["/ok"])

	def test_unparseable_selectors_are_reported_not_raised_by_the_engine(self):
		e = A.Engine()
		self.assertIsNone(e.matcher("div["))
		self.assertIsNone(e.matcher(":::bad"))
		self.assertTrue(callable(e.matcher(".ok")))
		self.assertIs(e.matcher(".ok"), e.matcher(".ok"))  # cached


class StubTests(unittest.TestCase):
	def test_stub_responses(self):
		self.assertEqual(A.stub_response("http://x/a.gif", "image")[1], "image/gif")
		self.assertTrue(A.stub_response("http://x/a.gif", "image")[0].startswith(b"GIF89a"))
		self.assertEqual(A.stub_response("http://x/a.css", "stylesheet"), (b"", "text/css"))
		self.assertEqual(A.stub_response("http://x/a.js", "script")[1], "application/javascript")
		self.assertIn(b"<html>", A.stub_response("http://x/f", "subdocument")[0])
		self.assertEqual(A.stub_response("http://x/x", "other")[1], "text/plain")

	def test_the_empty_gif_is_a_valid_image(self):
		import io
		try:
			from PIL import Image
		except ImportError:
			self.skipTest("Pillow not installed")
		img = Image.open(io.BytesIO(A.EMPTY_GIF))
		img.load()
		self.assertEqual(img.size, (1, 1))


class ManagerTests(unittest.TestCase):
	def make(self, **settings):
		cfg = SimpleNamespace(ADBLOCK=True, ADBLOCK_LISTS=[], **settings)
		return A.Manager(cfg)

	def test_disabled_by_default(self):
		self.assertFalse(A.Manager(SimpleNamespace()).enabled)
		self.assertIsNone(A.Manager(SimpleNamespace()).should_block("http://doubleclick.net/x", "script"))

	def test_builtin_rules_work_without_any_list(self):
		m = self.make()
		self.assertIsNotNone(m.should_block("http://ad.doubleclick.net/x.js", "script", "news.test"))
		self.assertIsNone(m.should_block("http://news.test/x.js", "script", "news.test"))

	def test_custom_rules(self):
		m = self.make(ADBLOCK_CUSTOM_RULES=["||mine.test^", "news.test##.mine"])
		self.assertIsNotNone(m.should_block("http://mine.test/x", "image"))
		self.assertIn(".mine", m.engine.selectors_for("news.test"))

	def test_allowlist_covers_the_domain_and_pages_on_it(self):
		m = self.make(ADBLOCK_ALLOWLIST=["doubleclick.net", "friend.test"])
		self.assertIsNone(m.should_block("http://ad.doubleclick.net/x", "script", "news.test"))
		self.assertIsNone(m.should_block("http://googlesyndication.com/x", "script", "www.friend.test"))
		self.assertIsNotNone(m.should_block("http://googlesyndication.com/x", "script", "news.test"))

	def test_counts_blocked_hosts(self):
		m = self.make()
		for _ in range(3):
			m.should_block("http://ad.doubleclick.net/x", "script", "news.test")
		self.assertEqual(m.blocked["ad.doubleclick.net"], 3)
		self.assertEqual(m.total_blocked, 3)

	def test_filter_page_counts(self):
		m = self.make()
		soup = BeautifulSoup('<body><img src="http://doubleclick.net/p.gif"><div class="adsbygoogle">x</div></body>', "html5lib")
		r = m.filter_page(soup, "http://news.test/")
		self.assertEqual((r["requests"], r["cosmetic"]), (1, 1))
		self.assertEqual(m.cosmetic_removed, 1)
		self.assertEqual(m.blocked["doubleclick.net"], 1)


class ListLoadingTests(unittest.TestCase):
	def setUp(self):
		self.dir = tempfile.TemporaryDirectory()

	def tearDown(self):
		self.dir.cleanup()

	def test_local_file_lists_are_loaded(self):
		path = os.path.join(self.dir.name, "mine.txt")
		with open(path, "w") as f:
			f.write("[Adblock Plus 2.0]\n||fromfile.test^\nsite.test##.fromfile\n")
		m = A.Manager(SimpleNamespace(ADBLOCK=True, ADBLOCK_LISTS=[path], ADBLOCK_CACHE_DIR=self.dir.name))
		m.rebuild()
		self.assertIsNotNone(m.should_block("http://fromfile.test/x", "script"))
		self.assertIn(".fromfile", m.engine.selectors_for("site.test"))

	def test_cached_download_is_used_without_network(self):
		source = "http://lists.invalid/list.txt"
		m = A.Manager(SimpleNamespace(ADBLOCK=True, ADBLOCK_LISTS=[source], ADBLOCK_CACHE_DIR=self.dir.name))
		with open(os.path.join(self.dir.name, A._cache_name(source)), "w") as f:
			f.write("||cached.test^\n")
		m.rebuild()
		self.assertIsNotNone(m.should_block("http://cached.test/x", "script"))

	def test_fresh_cache_is_not_downloaded_again(self):
		source = "http://lists.invalid/list.txt"
		m = A.Manager(SimpleNamespace(ADBLOCK=True, ADBLOCK_LISTS=[source], ADBLOCK_CACHE_DIR=self.dir.name, ADBLOCK_UPDATE_HOURS=24))
		with open(os.path.join(self.dir.name, A._cache_name(source)), "w") as f:
			f.write("||cached.test^\n")
		m._download = lambda s: self.fail("should not download a fresh list")
		self.assertEqual(m.refresh(), 0)

	def test_stale_cache_is_refreshed_and_failures_keep_the_old_list(self):
		source = "http://lists.invalid/list.txt"
		m = A.Manager(SimpleNamespace(ADBLOCK=True, ADBLOCK_LISTS=[source], ADBLOCK_CACHE_DIR=self.dir.name, ADBLOCK_UPDATE_HOURS=1))
		path = os.path.join(self.dir.name, A._cache_name(source))
		with open(path, "w") as f:
			f.write("||cached.test^\n")
		old = time.time() - 7200
		os.utime(path, (old, old))
		calls = []

		def failing(s):
			calls.append(s)
			raise OSError("offline")

		m._download = failing
		self.assertEqual(m.refresh(), 0)
		self.assertEqual(calls, [source])
		self.assertIn("offline", m.status[source]["error"])
		m.rebuild()
		self.assertIsNotNone(m.should_block("http://cached.test/x", "script"))  # still using the stale copy

	def test_html_error_pages_are_not_accepted_as_lists(self):
		self.assertFalse(A._looks_like_a_list("<!DOCTYPE html><html><body>Not found</body></html>"))
		self.assertFalse(A._looks_like_a_list(""))
		self.assertFalse(A._looks_like_a_list("! just a comment\n"))
		self.assertTrue(A._looks_like_a_list("[Adblock Plus 2.0]\n" + "\n".join(f"||h{i}.test^" for i in range(10))))


class PerformanceTests(unittest.TestCase):
	def test_matching_stays_fast_with_a_large_list(self):
		lines = [f"||tracker{i}.example{i % 50}.test^" for i in range(30000)]
		lines += [f"/ads{i}/banner_*" for i in range(3000)] + [f"##.ad-{i}" for i in range(3000)]
		e = engine(*lines)
		urls = ["http://news.test/story/12345.html", "http://cdn.news.test/img/a.png", "http://tracker7.example7.test/p.gif",
				"http://x.test/ads42/banner_1.gif"] * 250
		start = time.time()
		hits = sum(1 for u in urls if e.match(u, "image", "news.test"))
		elapsed = time.time() - start
		self.assertEqual(hits, 500)
		self.assertLess(elapsed / len(urls), 0.002, f"{elapsed / len(urls) * 1000:.2f} ms per URL")


if __name__ == "__main__":
	unittest.main()
