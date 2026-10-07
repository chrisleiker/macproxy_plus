import os
import sys
import unittest

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import ad_slots as S


def collapse(body):
	soup = BeautifulSoup(f"<html><body>{body}</body></html>", "html5lib")
	n = S.collapse(soup)
	return soup, n


class NameTests(unittest.TestCase):
	def test_words(self):
		self.assertEqual(S.words_of("ad-wrapper"), {"ad", "wrapper"})
		self.assertEqual(S.words_of("adSlot_top"), {"ad", "slot", "top"})
		self.assertEqual(S.words_of("download"), {"download"})

	def test_ad_like_names(self):
		for cls in ("ad", "ads", "ad-wrapper", "ad--hero", "advert", "advertisement", "adsbygoogle", "ad_slot", "adslot", "top-ad",
					"dfp-slot", "gpt-ad", "ad1", "ads2", "adSlot", "sponsored-box", "Ad-Container"):
			soup = BeautifulSoup(f'<div class="{cls}"></div>', "html5lib")
			self.assertTrue(S.looks_like_an_ad_slot(soup.div), cls)

	def test_words_that_merely_contain_ad(self):
		for cls in ("download", "header", "shadow", "loader", "read-more", "gradient", "address", "adapter", "headline", "ladder",
					"padding", "badge", "reading-list", "sponsorship", "advanced-search", "adventure", "admin"):
			soup = BeautifulSoup(f'<div class="{cls}"></div>', "html5lib")
			self.assertFalse(S.looks_like_an_ad_slot(soup.div), cls)

	def test_id_counts_too(self):
		self.assertTrue(S.looks_like_an_ad_slot(BeautifulSoup('<div id="div-gpt-ad-123"></div>', "html5lib").div))
		self.assertTrue(S.looks_like_an_ad_slot(BeautifulSoup('<div id="ad-top"></div>', "html5lib").div))

	def test_only_container_tags(self):
		self.assertFalse(S.looks_like_an_ad_slot(BeautifulSoup('<a class="ad" href="/x"></a>', "html5lib").a))
		self.assertFalse(S.looks_like_an_ad_slot(BeautifulSoup('<img class="ad" src="x.png">', "html5lib").img))


class CollapseTests(unittest.TestCase):
	def test_the_real_ars_shape(self):
		soup, n = collapse('<p>before</p><div class="ad-wrapper is-fullwidth is-hero"><div class="ad-wrapper-inner"><div class="ad ad--hero"></div></div></div><p>after</p>')
		self.assertEqual(n, 1)  # the outer slot takes the inner ones with it
		self.assertEqual(soup.body.get_text(strip=True), "beforeafter")
		self.assertIsNone(soup.find(class_="ad-wrapper-inner"))

	def test_empty_slots_of_various_kinds(self):
		soup, n = collapse('<ins class="adsbygoogle"></ins><div id="div-gpt-ad-1"> \n </div><aside class="advertisement"><!-- ad --></aside><p>keep</p>')
		self.assertEqual(n, 3)
		self.assertEqual(soup.body.get_text(strip=True), "keep")

	def test_scripts_and_noscript_do_not_count_as_content(self):
		soup, n = collapse('<div class="ad"><script>googletag.display("x")</script><noscript>enable js</noscript></div>')
		self.assertEqual(n, 1)

	def test_anything_visible_keeps_the_slot(self):
		for inner in ("Advertisement", '<img src="a.png">', '<iframe src="http://x.test/"></iframe>', "<a href='/x'>Buy now</a>",
					  "<video></video>", "<svg></svg>", "<button>x</button>", "<input>", "<canvas></canvas>", "<table></table>"):
			soup, n = collapse(f'<div class="ad">{inner}</div>')
			self.assertEqual(n, 0, inner)
			self.assertIsNotNone(soup.find(class_="ad"), inner)

	def test_slots_with_content_nested_in_empty_ones(self):
		soup, n = collapse('<div class="ad-wrapper"><div class="ad"><img src="a.png"></div></div>')
		self.assertEqual(n, 0)

	def test_other_empty_boxes_are_left_alone(self):
		soup, n = collapse('<div class="spacer"></div><div class="download"></div><div class="header-shadow"></div>')
		self.assertEqual(n, 0)

	def test_a_wrapper_that_held_only_the_slot_goes_too(self):
		soup, n = collapse('<div id="outer"><div class="ad-slot"></div></div><p>x</p>')
		self.assertEqual(n, 2)
		self.assertIsNone(soup.find(id="outer"))

	def test_a_wrapper_with_other_content_stays(self):
		soup, n = collapse('<div id="outer"><div class="ad-slot"></div><p>story</p></div>')
		self.assertEqual(n, 1)
		self.assertIsNotNone(soup.find(id="outer"))

	def test_a_wrapper_with_another_empty_box_stays(self):
		soup, n = collapse('<div id="outer"><div class="ad-slot"></div><div class="spacer"></div></div>')
		self.assertEqual(n, 1)
		self.assertIsNotNone(soup.find(id="outer"))

	def test_climbing_stops_at_page_structure(self):
		soup, n = collapse('<main><div class="ad"></div></main>')
		self.assertEqual(n, 1)
		self.assertIsNotNone(soup.find("main"))

	def test_climbs_at_most_two_levels(self):
		soup, n = collapse('<div id="a"><div id="b"><div id="c"><div class="ad"></div></div></div></div>')
		self.assertEqual(n, 3)
		self.assertIsNotNone(soup.find(id="a"))

	def test_nothing_to_do(self):
		soup, n = collapse("<p>hello</p>")
		self.assertEqual(n, 0)

	def test_many_slots(self):
		soup, n = collapse("".join('<div class="ad-slot"></div><p>x</p>' for _ in range(40)))
		self.assertEqual(n, 40)
		self.assertEqual(len(soup.find_all("p")), 40)


if __name__ == "__main__":
	unittest.main()
