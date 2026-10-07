import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import css_utils as C

OLD = C.CSSSettings(
	viewport_width=1024, viewport_height=768,
	unsupported_features=frozenset({"var", "calc", "minmax", "color-functions", "hexalpha", "rem", "viewport-units",
									"new-viewport-units", "gradients", "modern-color", "double-colon", "logical-properties", "inset"}),
	unsupported_properties=frozenset({"flex", "gap", "transform", "transition", "box-shadow", "aspect-ratio"}),
	unsupported_values={"display": {"flex", "grid", "inline-flex"}, "position": {"sticky"}},
	unsupported_selectors=(r":is\(", r":not\(", r":nth-", r"\[[^\]]*[\^$*]="),
	property_renames={"border-radius": ["-moz-border-radius"], "opacity": ["-moz-opacity", "opacity"]},
)
MODERN = C.CSSSettings(
	unsupported_features=frozenset({"minmax", "modern-color"}),
	unsupported_properties=frozenset({"aspect-ratio", "backdrop-filter"}),
)


def d(css, s=OLD, **kw):
	return C.downlevel_css(css, s, **kw).replace(" ", "")


class MediaTests(unittest.TestCase):
	def test_min_max_width(self):
		self.assertTrue(C.media_matches("(min-width: 768px)", OLD))
		self.assertFalse(C.media_matches("(max-width: 600px)", OLD))
		self.assertTrue(C.media_matches("screen and (min-width:900px) and (max-width:1100px)", OLD))

	def test_print_and_dark_do_not_match(self):
		self.assertFalse(C.media_matches("print", OLD))
		self.assertFalse(C.media_matches("(prefers-color-scheme: dark)", OLD))
		self.assertTrue(C.media_matches("(prefers-color-scheme: light)", OLD))

	def test_comma_is_or_and_not(self):
		self.assertTrue(C.media_matches("print, (min-width: 500px)", OLD))
		self.assertTrue(C.media_matches("not print", OLD))

	def test_range_syntax(self):
		self.assertTrue(C.media_matches("(width >= 600px)", OLD))
		self.assertFalse(C.media_matches("(width < 600px)", OLD))
		self.assertTrue(C.media_matches("(400px <= width <= 1200px)", OLD))

	def test_unknown_feature_false(self):
		self.assertFalse(C.media_matches("(foo: bar)", OLD))


class ValueTests(unittest.TestCase):
	def test_var_resolution_and_fallback(self):
		out = d(":root{--c:#123456;--gap:8px} a{color:var(--c);margin:var(--gap) var(--missing,2px)}")
		self.assertIn("a{color:#123456;margin:8px2px}", out)
		self.assertNotIn("--c", out)

	def test_var_undefined_drops_declaration(self):
		self.assertEqual(d("p{color:var(--nope);margin:0}"), "p{margin:0}")

	def test_var_inside_var(self):
		self.assertIn("color:red", d(":root{--a:var(--b);--b:red} p{color:var(--a)}"))

	def test_dark_theme_vars_not_used(self):
		css = ":root{--bg:#fff} @media (prefers-color-scheme: dark){:root{--bg:#000}} body{background:var(--bg)}"
		self.assertIn("background:#fff", d(css))

	def test_calc(self):
		self.assertIn("width:92px", d("p{width:calc(100px - 8px)}"))
		self.assertIn("width:48px", d("p{width:calc(2rem + 16px)}"))
		self.assertIn("width:512px", d("p{width:calc(50vw)}"))
		self.assertIn("width:30px", d("p{width:calc((10px + 20px) * 1)}"))

	def test_calc_with_percent_mix_dropped(self):
		self.assertEqual(d("p{width:calc(100% - 20px);margin:0}"), "p{margin:0}")

	def test_calc_pure_percent_kept(self):
		self.assertIn("width:50%", d("p{width:calc(100% / 2)}"))

	def test_calc_with_var(self):
		self.assertIn("width:30px", d(":root{--x:10px} p{width:calc(var(--x) * 3)}"))

	def test_min_max_clamp(self):
		self.assertIn("width:10px", d("p{width:min(10px, 20px)}"))
		self.assertIn("width:20px", d("p{width:max(10px, 20px)}"))
		self.assertIn("width:16px", d("p{width:clamp(1rem, 5px, 5rem)}"))

	def test_minmax_in_modern_profile(self):
		self.assertIn("width:10px", d("p{width:min(10px,20px)}", MODERN))

	def test_modern_profile_leaves_var_calc_alone(self):
		out = d("p{color:var(--x);width:calc(100% - 8px)}", MODERN)
		self.assertIn("var(--x)", out)
		self.assertIn("calc(100%-8px)", out)

	def test_units(self):
		self.assertIn("font-size:24px", d("p{font-size:1.5rem}"))
		self.assertIn("height:384px", d("p{height:50vh}"))
		self.assertIn("height:384px", d("p{height:50dvh}"))

	def test_rgba_blends_on_white(self):
		self.assertIn("color:#ff8080", d("p{color:rgba(255,0,0,0.5)}"))
		self.assertIn("color:#ff0000", d("p{color:rgb(255 0 0)}"))
		self.assertIn("color:transparent", d("p{color:rgba(0,0,0,0)}"))
		self.assertIn("color:#ff8080", d("p{color:rgb(255 0 0 / 50%)}"))

	def test_hsl(self):
		self.assertIn("color:#ff0000", d("p{color:hsl(0,100%,50%)}"))
		self.assertIn("color:#00ff00", d("p{color:hsl(120deg 100% 50%)}"))

	def test_hex_alpha(self):
		self.assertIn("color:#ff7f7f", d("p{color:#ff000080}"))
		self.assertIn("color:#123", d("p{color:#123}"))  # opaque short hex is left alone
		self.assertIn("color:#ff7777", d("p{color:#f008}"))

	def test_modern_color_dropped(self):
		self.assertEqual(d("p{color:oklch(60% 0.2 30);margin:0}"), "p{margin:0}")

	def test_gradient_falls_back_to_first_color(self):
		self.assertIn("background-color:#336699", d("p{background:linear-gradient(to right,#336699,#fff)}"))
		self.assertIn("background-color:#ff0000", d("p{background-image:linear-gradient(rgb(255,0,0),blue)}"))

	def test_gradient_layer_removed_keeps_url(self):
		out = d("p{background:url(a.png),linear-gradient(#fff,#000)}")
		self.assertIn("background:url(a.png)", out)
		self.assertIn("background-color:#fff", out)

	def test_important_preserved(self):
		self.assertIn("color:#ff0000!important", d("p{color:rgb(255,0,0)!important}"))


class PropertyTests(unittest.TestCase):
	def test_unsupported_property_and_value_dropped(self):
		self.assertEqual(d("p{display:flex;gap:4px;color:red}"), "p{color:red}")
		self.assertEqual(d("p{position:sticky;top:0}"), "p{top:0}")

	def test_vendor_prefix_dropped(self):
		self.assertEqual(d("p{-webkit-transition:all 1s;color:red}"), "p{color:red}")

	def test_renames(self):
		out = d("p{border-radius:4px;opacity:.5}")
		self.assertIn("-moz-border-radius:4px", out)
		self.assertIn("-moz-opacity:.5;opacity:.5", out)

	def test_inset_expansion_and_logical(self):
		self.assertIn("top:0;right:0;bottom:0;left:0", d("p{inset:0}"))
		self.assertIn("top:1px;right:2px;bottom:1px;left:2px", d("p{inset:1px 2px}"))
		self.assertIn("margin-left:4px", d("p{margin-inline-start:4px}"))

	def test_empty_rule_removed(self):
		self.assertEqual(d("p{display:flex}"), "")


class SelectorTests(unittest.TestCase):
	def test_unsupported_selector_dropped_others_kept(self):
		self.assertEqual(d("a:not(.x), b{color:red}"), "b{color:red}")
		self.assertEqual(d("li:nth-child(2){color:red}"), "")
		self.assertEqual(d('a[href^="http"]{color:red}'), "")

	def test_double_colon_rewritten(self):
		self.assertIn("a:before{content:'x'}", d("a::before{content:'x'}").replace('"', "'"))

	def test_modern_profile_keeps_selectors(self):
		self.assertIn("a:not(.x)", d("a:not(.x){color:red}", MODERN))


class AtRuleTests(unittest.TestCase):
	def test_media_unwrapped_when_matching_dropped_when_not(self):
		self.assertEqual(d("@media (min-width:600px){p{color:red}}"), "p{color:red}")
		self.assertEqual(d("@media (max-width:600px){p{color:red}}"), "")
		self.assertEqual(d("@media print{p{color:red}}"), "")

	def test_supports(self):
		self.assertEqual(d("@supports (display:grid){p{color:red}}"), "")
		self.assertEqual(d("@supports not (display:grid){p{color:red}}"), "p{color:red}")
		self.assertEqual(d("@supports (color:red){p{color:blue}}"), "p{color:blue}")

	def test_dropped_at_rules(self):
		css = "@charset 'utf-8'; @font-face{font-family:x;src:url(a.woff)} @keyframes k{from{top:0}to{top:1px}} p{color:red}"
		self.assertEqual(d(css), "p{color:red}")

	def test_layer_unwrapped(self):
		self.assertEqual(d("@layer base{p{color:red}}"), "p{color:red}")

	def test_nesting_flattened(self):
		out = d(".a{color:red; .b{color:blue} &:hover{color:green}}")
		self.assertEqual(out, ".a{color:red}\n.a.b{color:blue}\n.a:hover{color:green}".replace(".a.b", ".a.b"))

	def test_https_to_http(self):
		self.assertIn("url(http://x.com/a.png)", d("p{background:url(https://x.com/a.png)}"))

	def test_import_inlined(self):
		calls = []
		orig = C.fetch_text
		C.fetch_text = lambda url: (calls.append(url), "i{color:rgba(0,0,0,1)}")[1]
		try:
			out = d("@import url('sub/x.css'); p{color:red}", base_url="http://h.com/css/main.css")
		finally:
			C.fetch_text = orig
		self.assertEqual(calls, ["http://h.com/css/sub/x.css"])
		self.assertEqual(out, "i{color:#000000}\np{color:red}")

	def test_import_with_nonmatching_media_skipped(self):
		orig = C.fetch_text
		C.fetch_text = lambda url: self.fail("should not fetch")
		try:
			self.assertEqual(d("@import 'x.css' print;", base_url="http://h.com/"), "")
		finally:
			C.fetch_text = orig

	def test_import_depth_limit(self):
		orig = C.fetch_text
		C.fetch_text = lambda url: "@import 'again.css'; p{color:red}"
		try:
			out = d("@import 'a.css';", base_url="http://h.com/")
		finally:
			C.fetch_text = orig
		self.assertEqual(out.count("p{color:red}"), 3)


class InlineStyleTests(unittest.TestCase):
	def test_style_attribute(self):
		out = C.downlevel_declarations("color: rgba(255,0,0,.5); display:flex; width: calc(2rem)", OLD)
		self.assertEqual(out, "color:#ff8080;width:32px")

	def test_style_attribute_vars(self):
		out = C.downlevel_declarations("color: var(--c)", OLD, vars={"--c": "#abc"})
		self.assertEqual(out, "color:#abc")


class RobustnessTests(unittest.TestCase):
	def test_garbage_does_not_raise(self):
		for css in ["", "}}}{{{", "p{color:", "@media{", "p{width:calc(}", "@import;", "a{b:c d e f g}", "p{x:var()}"]:
			C.downlevel_css(css, OLD)

	def test_bytes_input(self):
		self.assertEqual(d("p{color:red}".encode()), "p{color:red}")


if __name__ == "__main__":
	unittest.main()


class SvgSelectorTwinTests(unittest.TestCase):
	"""Inline SVGs are turned into <img class="mp-svg">, so rules that style `svg` must reach those pictures too."""

	def css(self, source):
		return C.downlevel_css(source, OLD)

	def test_type_selector_gets_a_twin(self):
		self.assertEqual(self.css("svg{display:block}"), "svg, img.mp-svg{display:block}")

	def test_descendant_and_compound_subjects(self):
		self.assertEqual(self.css(".btn svg{margin:0}"), ".btn svg, .btn img.mp-svg{margin:0}")
		self.assertEqual(self.css("a>svg.icon{height:5px}"), "a>svg.icon, a>img.mp-svg.icon{height:5px}")
		self.assertEqual(self.css("svg#logo{width:9px}"), "svg#logo, img.mp-svg#logo{width:9px}")

	def test_selector_lists_and_media(self):
		self.assertEqual(self.css("h1, svg{margin:0}"), "h1, svg, img.mp-svg{margin:0}")
		self.assertEqual(self.css("@media (min-width:600px){.nav svg{height:20px}}"), ".nav svg, .nav img.mp-svg{height:20px}")

	def test_rules_about_the_inside_of_an_svg_are_left_alone(self):
		for css in ("svg path{fill:red}", "svg>g{fill:red}", ".a svg *{stroke:red}"):
			self.assertEqual(self.css(css), css)

	def test_other_selectors_are_unchanged(self):
		for css in (".svg{color:red}", "svgx{color:red}", "a.svg-icon{color:red}", "#svg{color:red}"):
			self.assertEqual(self.css(css), css)

	def test_nested_rules_get_twins_too(self):
		self.assertIn(".card img.mp-svg", self.css(".card{svg{width:10px}}"))
