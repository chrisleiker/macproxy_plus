import io
import os
import re
import sys
import types
import unittest

from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
	import flask  # noqa: F401
	import skia  # noqa: F401
	import PILSVG  # noqa: F401
	from PIL import Image
	HAVE_RENDERER = True
except ImportError:
	HAVE_RENDERER = False

from utils import css_utils as C
from utils import layout_utils as L
from utils import svg_utils as S

SETTINGS = C.CSSSettings(viewport_width=1024, viewport_height=768)

TAILWIND = """
.h-5{height:1.25rem}.w-5{width:1.25rem}.h-1{height:.25rem}
.h-\\[36px\\]{height:36px}.w-\\[109px\\]{width:109px}
@media (min-width:768px){.md\\:h-\\[65px\\]{height:65px}.md\\:w-\\[197px\\]{width:197px}}
.hidden{display:none}
@media (min-width:640px){.sm\\:hidden{display:none}}
.text-gray-300{--tw-text-opacity:1;color:rgb(158 160 170 / var(--tw-text-opacity))}
.text-red{color:#c80000}
.fill-white path{fill:#fff}
.block{display:block}
"""


def prepare_all(html, css=TAILWIND):
	soup = BeautifulSoup(f"<html><head><style>{css}</style></head><body>{html}</body></html>", "html5lib")
	cascade = L.StyleCascade(soup, "http://x.test/", SETTINGS)
	svgs = soup.find_all("svg")
	cascade.compute(S.elements_to_style(svgs))
	return soup, cascade, [S.prepare(svg, cascade) for svg in svgs]


def one(html, css=TAILWIND):
	return prepare_all(html, css)[2][0]


def attrs_of(markup):
	root = BeautifulSoup(markup, "html5lib").find("svg")
	return root


class ColourTests(unittest.TestCase):
	def test_hex_and_names(self):
		self.assertEqual(S.parse_color("#abc"), ("#aabbcc", 1.0))
		self.assertEqual(S.parse_color("#04CC74"), ("#04cc74", 1.0))
		self.assertEqual(S.parse_color("white"), ("white", 1.0))
		self.assertEqual(S.parse_color("none"), ("none", 1.0))
		self.assertEqual(S.parse_color("currentColor"), ("currentColor", 1.0))

	def test_functional_notations(self):
		self.assertEqual(S.parse_color("rgb(158 160 170 / 1)"), ("#9ea0aa", 1.0))
		self.assertEqual(S.parse_color("rgb(158, 160, 170)"), ("#9ea0aa", 1.0))
		self.assertEqual(S.parse_color("rgba(255,0,0,.5)"), ("#ff0000", 0.5))
		self.assertEqual(S.parse_color("rgb(255 0 0 / 25%)"), ("#ff0000", 0.25))
		self.assertEqual(S.parse_color("hsl(120 100% 50%)"), ("#00ff00", 1.0))
		self.assertEqual(S.parse_color("#ff000080"), ("#ff0000", 128 / 255))

	def test_gradients_keep_their_case(self):
		self.assertEqual(S.parse_color("url(#GradA)"), ("url(#GradA)", 1.0))

	def test_junk(self):
		for bad in ("", "rgb(1 2)", "#12", "12px"):
			self.assertIsNone(S.parse_color(bad), bad)


class LengthTests(unittest.TestCase):
	def test_lengths(self):
		self.assertEqual(S.length_px("20px"), 20)
		self.assertEqual(S.length_px("1.25rem"), 20)
		self.assertEqual(S.length_px("2em", 10), 20)
		self.assertEqual(S.length_px("24"), 24)
		self.assertEqual(S.length_px("12pt"), 16)
		for unknown in ("50%", "auto", "calc(1px + 2px)", "", None):
			self.assertIsNone(S.length_px(unknown), unknown)

	def test_viewbox(self):
		self.assertEqual(S.parse_viewbox("0 0 436 144.1"), (0, 0, 436, 144.1))
		self.assertEqual(S.parse_viewbox("0,0,20,20"), (0, 0, 20, 20))
		self.assertIsNone(S.parse_viewbox("0 0 0 0"))
		self.assertIsNone(S.parse_viewbox("nonsense"))


class SizeTests(unittest.TestCase):
	def test_sized_by_utility_classes(self):
		p = one('<svg class="h-5 w-5" viewBox="0 0 40 40"><path d="M0 0h40v40z"/></svg>')
		self.assertEqual((p.width, p.height), (20, 20))

	def test_arbitrary_values_and_media_queries_use_the_presets_viewport(self):
		# the logo: 36x109 normally, 65x197 from 768px up; the preset viewport is 1024 wide
		p = one('<svg class="h-[36px] w-[109px] md:h-[65px] md:w-[197px]" viewBox="0 0 436 144.1"><path d="M0 0z"/></svg>')
		self.assertEqual((p.width, p.height), (197, 65))

	def test_one_dimension_keeps_the_aspect_ratio(self):
		p = one('<svg class="h-1" viewBox="0 0 40 19.3"><path d="M0 0z"/></svg>')
		self.assertEqual(p.height, 4)
		self.assertEqual(p.width, 8)  # 4 * 40 / 19.3

	def test_attributes_when_css_says_nothing(self):
		p = one('<svg width="30" height="12" viewBox="0 0 60 24"><path d="M0 0z"/></svg>')
		self.assertEqual((p.width, p.height), (30, 12))

	def test_viewbox_when_nothing_else_does(self):
		p = one('<svg viewBox="0 0 24 16"><path d="M0 0z"/></svg>')
		self.assertEqual((p.width, p.height), (24, 16))

	def test_css_beats_attributes(self):
		p = one('<svg class="w-5" width="99" height="99" viewBox="0 0 10 10"><path d="M0 0z"/></svg>')
		self.assertEqual((p.width, p.height), (20, 99))

	def test_em_units_follow_the_font_size(self):
		p = one('<div style="font-size:10px"><svg class="i" viewBox="0 0 8 8"><path d="M0 0z"/></svg></div>', ".i{width:2em;height:2em}")
		self.assertEqual((p.width, p.height), (20, 20))

	def test_inline_style_on_the_svg(self):
		p = one('<svg style="width:30px;height:15px" viewBox="0 0 8 8"><path d="M0 0z"/></svg>')
		self.assertEqual((p.width, p.height), (30, 15))

	def test_max_width(self):
		p = one('<svg class="m" viewBox="0 0 100 50"><path d="M0 0z"/></svg>', ".m{width:100px;max-width:40px}")
		self.assertEqual((p.width, p.height), (40, 20))

	def test_percentages_fall_back(self):
		p = one('<svg class="f" viewBox="0 0 24 16"><path d="M0 0z"/></svg>', ".f{width:100%}")
		self.assertEqual((p.width, p.height), (24, 16))

	def test_raster_is_drawn_at_twice_the_displayed_size(self):
		p = one('<svg class="h-5 w-5" viewBox="0 0 40 40"><path d="M0 0z"/></svg>')
		root = attrs_of(p.markup)
		self.assertEqual((root["width"], root["height"]), ("40", "40"))

	def test_huge_sizes_are_capped(self):
		p = one('<svg class="b" viewBox="0 0 10 10"><path d="M0 0z"/></svg>', ".b{width:900px;height:900px}")
		self.assertEqual((p.width, p.height), (900, 900))
		self.assertLessEqual(float(attrs_of(p.markup)["width"]), S.MAX_RASTER)


class HiddenTests(unittest.TestCase):
	def test_hidden_class_drops_the_svg(self):
		self.assertIsNone(one('<svg class="hidden h-5" viewBox="0 0 8 8"><path d="M0 0z"/></svg>'))

	def test_media_query_hiding(self):
		self.assertIsNone(one('<svg class="sm:hidden h-5" viewBox="0 0 8 8"><path d="M0 0z"/></svg>'))

	def test_inline_display_none(self):
		self.assertIsNone(one('<svg style="display:none" viewBox="0 0 8 8"><path d="M0 0z"/></svg>'))

	def test_hidden_parent(self):
		self.assertIsNone(one('<div class="hidden"><svg viewBox="0 0 8 8"><path d="M0 0z"/></svg></div>'))

	def test_sprite_sheets_are_dropped(self):
		self.assertIsNone(one('<svg viewBox="0 0 8 8"><defs><symbol id="a"><path d="M0 0h8v8z"/></symbol></defs></svg>'))

	def test_zero_sized_is_dropped(self):
		self.assertIsNone(one('<svg width="0" height="0" viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg>'))

	def test_a_rotated_absolutely_placed_decoration_is_dropped(self):
		for css in ("position:absolute;transform:rotate(-90deg)", "position:fixed;transform:matrix(0,-1,1,0,0,0)"):
			self.assertIsNone(one(f'<svg style="{css}" viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg>'), css)

	def test_a_rotated_or_an_absolute_one_alone_is_kept(self):
		self.assertIsNotNone(one('<svg style="transform:rotate(90deg)" width="8" height="8" viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg>'))
		self.assertIsNotNone(one('<svg style="position:absolute;transform:translateX(4px)" width="8" height="8" viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg>'))
		self.assertIsNotNone(one('<svg style="position:absolute" width="8" height="8" viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg>'))

	def test_visible_ones_in_the_same_page_are_kept(self):
		_, _, results = prepare_all('<svg class="hidden" viewBox="0 0 8 8"><path d="M0 0z"/></svg>'
									'<svg class="h-5 w-5" viewBox="0 0 8 8"><path d="M0 0z"/></svg>')
		self.assertIsNone(results[0])
		self.assertIsNotNone(results[1])


class PaintTests(unittest.TestCase):
	def test_current_colour_comes_from_the_css_color(self):
		p = one('<svg class="text-gray-300" viewBox="0 0 8 8"><path fill="currentColor" d="M0 0h8v8z"/></svg>')
		self.assertEqual(attrs_of(p.markup)["color"], "#9ea0aa")

	def test_colour_is_inherited_from_an_ancestor(self):
		p = one('<div class="text-red"><span><svg viewBox="0 0 8 8"><path d="M0 0z"/></svg></span></div>')
		self.assertEqual(attrs_of(p.markup)["color"], "#c80000")

	def test_default_colour_is_black(self):
		self.assertEqual(attrs_of(one('<svg viewBox="0 0 8 8"><path d="M0 0z"/></svg>').markup)["color"], "#000000")

	def test_the_svgs_own_colour_attribute_is_a_fallback(self):
		self.assertEqual(attrs_of(one('<svg color="#112233" viewBox="0 0 8 8"><path d="M0 0z"/></svg>').markup)["color"], "#112233")

	def test_css_fill_on_inner_elements_is_baked_in(self):
		p = one('<svg class="fill-white" viewBox="0 0 8 8"><path d="M0 0h8v8z"/><circle r="2"/></svg>')
		root = attrs_of(p.markup)
		self.assertEqual(root.find("path")["fill"], "#ffffff")
		self.assertNotIn("fill", root.find("circle").attrs)

	def test_inline_style_paint_beats_attributes(self):
		p = one('<svg viewBox="0 0 8 8"><path fill="blue" style="fill:#ff0000;stroke:rgba(0,0,0,.5)" d="M0 0z"/></svg>')
		path = attrs_of(p.markup).find("path")
		self.assertEqual((path["fill"], path["stroke"], path["stroke-opacity"]), ("#ff0000", "#000000", "0.5"))

	def test_gradient_references_keep_their_case(self):
		p = one('<svg viewBox="0 0 8 8"><defs><linearGradient id="GradA"/></defs><path style="fill:url(#GradA)" d="M0 0z"/></svg>')
		self.assertEqual(attrs_of(p.markup).find("path")["fill"], "url(#GradA)")

	def test_transparent_becomes_none(self):
		p = one('<svg viewBox="0 0 8 8"><path style="fill:transparent" d="M0 0z"/></svg>')
		self.assertEqual(attrs_of(p.markup).find("path")["fill"], "none")

	def test_class_and_style_are_removed_from_the_drawn_svg(self):
		p = one('<svg class="h-5 w-5" style="width:20px" viewBox="0 0 8 8"><g class="x"><path d="M0 0z"/></g></svg>')
		root = attrs_of(p.markup)
		self.assertNotIn("class", root.attrs)
		self.assertNotIn("style", root.attrs)
		self.assertNotIn("class", root.find("g").attrs)

	def test_the_original_class_and_id_are_reported_for_the_img(self):
		p = one('<svg id="logo" class="h-5 w-5" viewBox="0 0 8 8"><path d="M0 0z"/></svg>')
		self.assertEqual((p.attrs["id"], p.attrs["class"]), ("logo", ["h-5", "w-5"]))

	def test_namespace_and_viewbox_are_added_when_missing(self):
		p = one('<svg width="30" height="10"><path d="M0 0z"/></svg>')
		root = attrs_of(p.markup)
		self.assertEqual(root["xmlns"], S.SVG_NS)
		self.assertEqual(root["viewbox"] if "viewbox" in root.attrs else root["viewBox"], "0 0 30 10")


@unittest.skipUnless(HAVE_RENDERER, "needs Skia (pillow-svg), Flask and Pillow")
class DrawingTests(unittest.TestCase):
	"""The prepared markup, drawn by the real renderer."""

	@classmethod
	def setUpClass(cls):
		from utils import image_utils
		cls.image_utils = image_utils

	def draw(self, prepared, **kw):
		options = dict(resize=False, convert=False, convert_to=None, dithering=None, keep_alpha=True)
		options.update(kw)
		options.setdefault("svg_size", prepared.raster)
		data = self.image_utils.optimize_image(prepared.markup.encode(), **options)
		return Image.open(io.BytesIO(data)).convert("RGBA")

	def test_current_colour_is_what_the_css_says_and_the_background_is_transparent(self):
		p = one('<svg class="h-5 w-5 text-red" viewBox="0 0 10 10"><path fill="currentColor" d="M2 2h6v6H2z"/></svg>')
		img = self.draw(p)
		self.assertEqual(img.size, (40, 40))  # 20px displayed, drawn at twice that
		self.assertEqual(img.getpixel((20, 20)), (200, 0, 0, 255))
		self.assertEqual(img.getpixel((1, 1))[3], 0)

	def test_white_on_transparent_survives(self):
		p = one('<svg class="h-5 w-5 fill-white" viewBox="0 0 10 10"><path d="M0 0h10v10z"/></svg>')
		img = self.draw(p)
		self.assertEqual(img.getpixel((30, 10)), (255, 255, 255, 255))  # inside the triangle (x > y)
		self.assertEqual(img.getpixel((10, 30))[3], 0)  # outside it

	def test_transparency_is_flattened_when_the_output_cannot_hold_it(self):
		p = one('<svg class="h-5 w-5" viewBox="0 0 10 10"><path d="M2 2h6v6H2z"/></svg>')
		img = self.draw(p, convert=True, convert_to="gif")
		self.assertEqual(img.getpixel((1, 1))[:3], (255, 255, 255))

	def test_logo_size_and_orientation(self):
		p = one('<svg class="h-[36px] w-[109px] md:h-[65px] md:w-[197px]" viewBox="0 0 436 144.1"><rect width="436" height="144.1" fill="#ff4e00"/></svg>')
		img = self.draw(p)
		self.assertEqual((p.width, p.height), (197, 65))
		self.assertEqual(img.size, (394, 130))
		self.assertEqual(img.getpixel((200, 60)), (255, 78, 0, 255))


@unittest.skipUnless(HAVE_RENDERER, "needs Skia (pillow-svg), Flask and Pillow")
class PageTests(unittest.TestCase):
	"""transcode_html end to end: the SVG becomes an <img> that carries what the page's CSS needs."""

	@classmethod
	def setUpClass(cls):
		cls.stubbed = "config" not in sys.modules
		if cls.stubbed:
			sys.modules["config"] = types.SimpleNamespace(
				PRESET=None, CONVERT_IMAGES=True, CONVERT_IMAGES_TO_FILETYPE="png", RESIZE_IMAGES=False,
				MAX_IMAGE_WIDTH=None, MAX_IMAGE_HEIGHT=None, DITHERING_ALGORITHM=None)
		from flask import Flask
		from utils import html_utils, image_utils
		cls.html_utils, cls.image_utils = html_utils, image_utils
		# Other test modules may already have loaded html_utils with the real config (1-bit GIF output); use PNG settings
		cfg = html_utils.config
		wanted = dict(CONVERT_IMAGES=True, CONVERT_IMAGES_TO_FILETYPE="png", RESIZE_IMAGES=False, MAX_IMAGE_WIDTH=None,
					  MAX_IMAGE_HEIGHT=None, DITHERING_ALGORITHM=None, IMAGE_SCALE_PERCENT=None, SVG_STYLES=True)
		cls.saved_config = {k: (getattr(cfg, k) if hasattr(cfg, k) else cls) for k in wanted}  # `cls` marks "was not set"
		for k, v in wanted.items():
			setattr(cfg, k, v)
		# The proxy empties the picture cache every time it starts; do the same so no stale picture is mistaken for ours
		import shutil
		shutil.rmtree(image_utils.CACHE_DIR, ignore_errors=True)
		os.makedirs(image_utils.CACHE_DIR, exist_ok=True)
		cls.app = Flask(__name__)
		cls.app.config["MACPROXY_HOST_AND_PORT"] = "proxy.test:5001"

		@cls.app.route("/cached_image/<path:filename>")
		def serve_cached_image(filename):
			return ""

	@classmethod
	def tearDownClass(cls):
		cfg = cls.html_utils.config
		for k, v in cls.saved_config.items():
			if v is cls:
				if hasattr(cfg, k):
					delattr(cfg, k)
			else:
				setattr(cfg, k, v)
		if cls.stubbed:
			sys.modules.pop("config", None)
			sys.modules.pop("utils.html_utils", None)

	def transcode(self, body, css=TAILWIND, **flags):
		html = f"<html><head><style>{css}</style></head><body>{body}</body></html>"
		cfg = self.html_utils.config
		before = {k: getattr(cfg, k) for k in flags}
		for name, value in flags.items():
			setattr(cfg, name, value)
		try:
			with self.app.test_request_context():
				return self.html_utils.transcode_html(html, "http://example.test/", whitelisted_domains=[], simplify_html=False,
													  tags_to_unwrap=[], tags_to_strip=[], attributes_to_strip=[],
													  convert_characters=False, conversion_table={}).decode()
		finally:
			for name, value in before.items():
				setattr(cfg, name, value)

	def cached_image(self, out):
		name = re.search(r'/cached_image/([\w.]+)', out).group(1)
		return Image.open(os.path.join(self.image_utils.CACHE_DIR, name))

	def test_svg_becomes_a_correctly_sized_img_with_the_classes_kept(self):
		out = self.transcode('<svg id="i1" class="h-5 w-5 text-red" viewBox="0 0 40 40" aria-label="Search"><path fill="currentColor" d="M5 5h30v30H5z"/></svg>')
		img = BeautifulSoup(out, "html5lib").find("img")
		self.assertEqual((img["width"], img["height"]), ("20", "20"))
		self.assertEqual(img["class"], ["h-5", "w-5", "text-red", "mp-svg"])
		self.assertEqual((img["id"], img["alt"]), ("i1", "Search"))
		self.assertNotIn("<svg", out)
		pic = self.cached_image(out)
		self.assertEqual(pic.size, (40, 40))
		self.assertEqual(pic.convert("RGBA").getpixel((20, 20)), (200, 0, 0, 255))

	def test_hidden_svgs_vanish_and_visible_ones_stay(self):
		out = self.transcode('<svg class="hidden h-5 w-5" viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg>'
							 '<svg class="sm:hidden" viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg>'
							 '<svg class="h-5 w-5" viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg>')
		self.assertEqual(len(BeautifulSoup(out, "html5lib").find_all("img")), 1)

	def test_transparency_is_kept_in_png_output(self):
		out = self.transcode('<svg class="h-5 w-5 fill-white" viewBox="0 0 10 10"><path d="M0 0h10v10z"/></svg>')
		pic = self.cached_image(out).convert("RGBA")
		self.assertEqual(pic.getpixel((30, 10)), (255, 255, 255, 255))
		self.assertEqual(pic.getpixel((10, 30))[3], 0)

	def test_sprite_sheet_is_dropped(self):
		out = self.transcode('<svg style="display:none"><symbol id="a"><path d="M0 0h8v8z"/></symbol></svg><p>kept</p>')
		self.assertNotIn("<img", out)
		self.assertIn("<p>kept</p>", out)

	def test_svgs_still_convert_without_the_style_engine(self):
		out = self.transcode('<svg class="h-5 w-5" width="12" height="12" viewBox="0 0 8 8"><path d="M0 0h8v8z"/></svg>', SVG_STYLES=False)
		img = BeautifulSoup(out, "html5lib").find("img")
		self.assertEqual((img["width"], img["height"]), ("12", "12"))  # the old behaviour: the SVG's own attributes
		self.assertIn("mp-svg", img["class"])

	def test_svg_without_any_size_information_still_converts(self):
		out = self.transcode('<svg><circle r="5" cx="5" cy="5"/></svg>')
		self.assertIn("<img", out)


if __name__ == "__main__":
	unittest.main()
