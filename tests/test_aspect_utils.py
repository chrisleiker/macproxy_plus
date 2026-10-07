import os
import sys
import types
import unittest
from types import SimpleNamespace

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
	import flask  # noqa: F401
	import skia  # noqa: F401
	import PILSVG  # noqa: F401
	HAVE_STACK = True
except ImportError:
	HAVE_STACK = False

from utils import aspect_utils as A
from utils import css_utils as C
from utils import layout_utils as L

SETTINGS = C.CSSSettings(viewport_width=1024, viewport_height=768, unsupported_properties=frozenset({"aspect-ratio"}))
CSS = """
.aspect-video{aspect-ratio:16/9}.aspect-square{aspect-ratio:1/1}.aspect-\\[4\\/3\\]{aspect-ratio:4/3}
.relative{position:relative}.absolute{position:absolute}.block{display:block}.inline{display:inline}
.h-40{height:10rem}.hidden{display:none}.w-full{width:100%}.cell{display:table-cell}
"""


def run(body, css=CSS):
	soup = BeautifulSoup(f"<html><head><style>{css}</style></head><body>{body}</body></html>", "html5lib")
	cascade = L.StyleCascade(soup, "http://x.test/", SETTINGS)
	count = A.emulate(soup, cascade, SETTINGS)
	return soup, count


def style_of(el):
	return dict(p.split(":", 1) for p in (el.get("style") or "").split(";") if p)


class ParseTests(unittest.TestCase):
	def test_ratios(self):
		self.assertEqual(A.parse_ratio("16/9"), (16.0, 9.0))
		self.assertEqual(A.parse_ratio("16 / 9"), (16.0, 9.0))
		self.assertEqual(A.parse_ratio("1"), (1.0, 1.0))
		self.assertEqual(A.parse_ratio("auto 4/3"), (4.0, 3.0))
		self.assertEqual(A.parse_ratio("1.5"), (1.5, 1.0))

	def test_unusable(self):
		for bad in ("auto", "", None, "0/1", "1/0", "a/b", "16/9/2", "-1"):
			self.assertIsNone(A.parse_ratio(bad), bad)

	def test_padding(self):
		self.assertEqual(A._padding((16, 9)), "56.25%")
		self.assertEqual(A._padding((1, 1)), "100%")
		self.assertEqual(A._padding((4, 3)), "75%")
		self.assertEqual(A._padding((3, 2)), "66.6667%")


class EnabledTests(unittest.TestCase):
	def cfg(self, **kw):
		base = dict(CSS_MODE="downlevel", CSS_UNSUPPORTED_PROPERTIES=["aspect-ratio", "flex"])
		base.update(kw)
		return SimpleNamespace(**base)

	def test_on_for_a_browser_without_the_property(self):
		self.assertTrue(A.enabled(self.cfg()))

	def test_off_when_the_browser_supports_it_or_css_is_stripped(self):
		self.assertFalse(A.enabled(self.cfg(CSS_UNSUPPORTED_PROPERTIES=["flex"])))
		self.assertFalse(A.enabled(self.cfg(CSS_MODE="strip")))
		self.assertFalse(A.enabled(SimpleNamespace()))

	def test_can_be_switched_off(self):
		self.assertFalse(A.enabled(self.cfg(ASPECT_RATIO_EMULATION=False)))


class BoxTests(unittest.TestCase):
	def test_box_with_only_an_absolute_child_gets_a_padding_height(self):
		soup, n = run('<div id="b" class="relative aspect-video w-full"><img class="absolute" src="a.png"></div>')
		st = style_of(soup.find(id="b"))
		self.assertEqual((n, st["height"], st["padding-bottom"]), (1, "0", "56.25%"))

	def test_the_real_ars_shape_an_inline_link_around_an_absolute_picture(self):
		soup, n = run('<div id="b" class="relative block aspect-video"><a href="/x"><img class="absolute" src="a.png"></a></div>')
		self.assertEqual(n, 1)
		self.assertEqual(style_of(soup.find(id="b"))["padding-bottom"], "56.25%")

	def test_empty_box(self):
		soup, n = run('<div id="b" class="aspect-square"></div>')
		self.assertEqual((n, style_of(soup.find(id="b"))["padding-bottom"]), (1, "100%"))

	def test_arbitrary_ratio_class(self):
		soup, _ = run('<div id="b" class="aspect-[4/3]"></div>')
		self.assertEqual(style_of(soup.find(id="b"))["padding-bottom"], "75%")

	def test_inline_style_ratio(self):
		soup, n = run('<div id="b" style="aspect-ratio: 3 / 2"><span style="position:absolute">x</span></div>', css="")
		self.assertEqual((n, style_of(soup.find(id="b"))["padding-bottom"]), (1, "66.6667%"))

	def test_an_inline_element_becomes_a_block(self):
		soup, _ = run('<a id="b" class="aspect-video" href="/x"><img class="absolute" src="a.png"></a>')
		self.assertEqual(style_of(soup.find(id="b"))["display"], "block")

	def test_a_table_cell_gets_an_inner_box_instead(self):
		soup, n = run('<div id="b" class="aspect-video cell"><img class="absolute" src="a.png"></div>')
		outer = soup.find(id="b")
		self.assertEqual(n, 1)
		self.assertNotIn("padding-bottom", style_of(outer))
		inner = outer.find("div", recursive=False)
		self.assertEqual((style_of(inner)["padding-bottom"], style_of(inner)["position"]), ("56.25%", "relative"))
		self.assertEqual(inner.find("img")["src"], "a.png")  # the children moved inside it

	def test_a_cell_made_by_the_layout_emulation(self):
		soup, n = run('<div id="b" class="aspect-video" style="display:table-cell;float:none"><img class="absolute" src="a.png"></div>')
		self.assertEqual(n, 1)
		self.assertIsNotNone(soup.find(id="b").find("div", recursive=False))


class LeftAloneTests(unittest.TestCase):
	def test_in_flow_text_or_pictures(self):
		for inner in ("some text", '<img src="a.png">', "<p>para</p>", "<div>block</div>", '<a href="/x">link text</a>',
					  '<a href="/x"><img src="a.png"></a>', "<button>b</button>"):
			soup, n = run(f'<div id="b" class="aspect-video">{inner}</div>')
			self.assertEqual(n, 0, inner)
			self.assertNotIn("style", soup.find(id="b").attrs)

	def test_a_box_with_its_own_height(self):
		soup, n = run('<div id="b" class="aspect-video h-40"><img class="absolute" src="a.png"></div>')
		self.assertEqual(n, 0)

	def test_hidden_boxes(self):
		self.assertEqual(run('<div class="aspect-video hidden"><img class="absolute" src="a.png"></div>')[1], 0)

	def test_images_and_other_replaced_pictures_keep_their_own_ratio(self):
		for tag in ("img", "svg", "picture", "table", "input", "button"):
			soup, n = run(f'<{tag} class="aspect-video"></{tag}>')
			self.assertEqual(n, 0, tag)

	def test_unusable_ratio(self):
		soup, n = run('<div class="a"></div>', css=".a{aspect-ratio:auto}")
		self.assertEqual(n, 0)

	def test_nothing_to_do(self):
		soup, n = run("<p>hello</p>")
		self.assertEqual(n, 0)

	def test_comments_and_whitespace_do_not_count_as_content(self):
		soup, n = run('<div id="b" class="aspect-video"> \n <!-- note --> <img class="absolute" src="a.png"> \n </div>')
		self.assertEqual(n, 1)

	def test_out_of_flow_text_does_not_count(self):
		soup, n = run('<div id="b" class="aspect-video"><span class="absolute">caption</span></div>')
		self.assertEqual(n, 1)


class ReplacedTests(unittest.TestCase):
	def test_iframe_is_wrapped_in_a_ratio_box(self):
		soup, n = run('<iframe id="f" class="aspect-video w-full" src="http://v.test/e"></iframe>')
		frame = soup.find(id="f")
		wrapper = frame.parent
		self.assertEqual(n, 1)
		self.assertEqual((style_of(wrapper)["position"], style_of(wrapper)["height"], style_of(wrapper)["padding-bottom"]), ("relative", "0", "56.25%"))
		st = style_of(frame)
		self.assertEqual((st["position"], st["width"], st["height"], st["top"], st["left"]), ("absolute", "100%", "100%", "0", "0"))

	def test_fixed_width_is_kept_on_the_box(self):
		soup, _ = run('<video id="v" class="aspect-video" src="m.mp4"></video>', css=CSS + ".aspect-video{width:400px}")
		self.assertEqual(style_of(soup.find(id="v").parent)["width"], "400px")
		self.assertEqual(style_of(soup.find(id="v").parent)["max-width"], "100%")

	def test_a_replaced_element_with_a_height_is_left_alone(self):
		soup, n = run('<iframe class="aspect-video h-40" src="http://v.test/e"></iframe>')
		self.assertEqual(n, 0)
		self.assertEqual(soup.find("iframe").parent.name, "body")

	def test_other_replaced_tags(self):
		for tag in ("video", "canvas", "embed", "object", "iframe"):
			soup, n = run(f'<{tag} class="aspect-square"></{tag}>')
			self.assertEqual(n, 1, tag)


class FindingTests(unittest.TestCase):
	def test_elements_with_a_property(self):
		soup = BeautifulSoup(f"<html><head><style>{CSS}</style></head><body><div class='aspect-video'></div><p class='block'></p>"
							 "<div style='aspect-ratio:2/1'></div></body></html>", "html5lib")
		cascade = L.StyleCascade(soup, "http://x.test/", SETTINGS)
		names = sorted(el.get("class", [""])[0] if el.get("class") else "inline-style" for el in cascade.elements_with("aspect-ratio", soup))
		self.assertEqual(names, ["aspect-video", "inline-style"])


@unittest.skipUnless(HAVE_STACK, "needs Flask and pillow-svg (available in the Docker image)")
class PageTests(unittest.TestCase):
	"""transcode_html end to end, configured like the Classilla preset."""

	@classmethod
	def setUpClass(cls):
		cls.stubbed = "config" not in sys.modules
		if cls.stubbed:
			sys.modules["config"] = types.SimpleNamespace(
				PRESET=None, CONVERT_IMAGES=False, CONVERT_IMAGES_TO_FILETYPE=None, RESIZE_IMAGES=False,
				MAX_IMAGE_WIDTH=None, MAX_IMAGE_HEIGHT=None, DITHERING_ALGORITHM=None)
		from flask import Flask
		from utils import html_utils
		cls.html_utils = html_utils
		cls.app = Flask(__name__)
		cls.app.config["MACPROXY_HOST_AND_PORT"] = "proxy.test:5001"
		cls.wanted = dict(CSS_MODE="downlevel", CSS_UNSUPPORTED_PROPERTIES=["aspect-ratio"], CSS_VIEWPORT_WIDTH=1024,
						  CSS_VIEWPORT_HEIGHT=768, CSS_UNSUPPORTED_FEATURES=[], CSS_UNSUPPORTED_VALUES={}, CSS_UNSUPPORTED_SELECTORS=[],
						  CSS_PROPERTY_RENAMES={}, LAYOUT_EMULATION=False, SVG_STYLES=True, ASPECT_RATIO_EMULATION=True)
		cfg = html_utils.config
		cls.saved = {k: (getattr(cfg, k) if hasattr(cfg, k) else cls) for k in cls.wanted}
		for k, v in cls.wanted.items():
			setattr(cfg, k, v)

	@classmethod
	def tearDownClass(cls):
		cfg = cls.html_utils.config
		for k, v in cls.saved.items():
			if v is cls:
				if hasattr(cfg, k):
					delattr(cfg, k)
			else:
				setattr(cfg, k, v)
		if cls.stubbed:
			sys.modules.pop("config", None)
			sys.modules.pop("utils.html_utils", None)

	def transcode(self, body, **flags):
		html = f"<html><head><style>{CSS}</style></head><body>{body}</body></html>"
		cfg = self.html_utils.config
		before = {k: getattr(cfg, k, None) for k in flags}
		for k, v in flags.items():
			setattr(cfg, k, v)
		try:
			with self.app.test_request_context():
				return self.html_utils.transcode_html(html, "http://example.test/", whitelisted_domains=[], simplify_html=False,
													  tags_to_unwrap=[], tags_to_strip=[], attributes_to_strip=[],
													  convert_characters=False, conversion_table={}).decode()
		finally:
			for k, v in before.items():
				setattr(cfg, k, v)

	BOX = '<div id="b" class="relative block aspect-video"><a href="/x"><img class="absolute" src="a.png"></a></div>'

	def test_the_box_gets_its_height_in_the_delivered_page(self):
		out = self.transcode(self.BOX)
		box = BeautifulSoup(out, "html5lib").find(id="b")
		self.assertEqual(style_of(box)["padding-bottom"], "56.25%")
		self.assertEqual(style_of(box)["height"], "0")

	def test_the_property_itself_is_still_removed_from_the_css(self):
		out = self.transcode(self.BOX)
		self.assertNotIn("aspect-ratio", out)

	def test_off_when_the_browser_supports_the_property(self):
		out = self.transcode(self.BOX, CSS_UNSUPPORTED_PROPERTIES=[])
		self.assertNotIn("padding-bottom", out)

	def test_off_with_the_switch(self):
		self.assertNotIn("padding-bottom", self.transcode(self.BOX, ASPECT_RATIO_EMULATION=False))

	def test_pages_without_aspect_ratio_are_untouched(self):
		out = self.transcode('<p id="p">hello</p>')
		self.assertNotIn("style=", BeautifulSoup(out, "html5lib").find(id="p").__str__())

	def test_an_iframe_is_wrapped(self):
		out = self.transcode('<iframe id="f" class="aspect-video w-full" src="http://v.test/e"></iframe>')
		frame = BeautifulSoup(out, "html5lib").find(id="f")
		self.assertEqual(style_of(frame.parent)["padding-bottom"], "56.25%")


if __name__ == "__main__":
	unittest.main()
