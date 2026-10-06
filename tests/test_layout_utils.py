import os
import sys
import unittest

import soupsieve
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from utils import css_utils as C
from utils import layout_utils as L

SETTINGS = C.CSSSettings(viewport_width=1000, viewport_height=800)


def run(css, body):
	soup = BeautifulSoup(f"<html><head><style>{css}</style></head><body>{body}</body></html>", "html5lib")
	L.emulate_layout(soup, "http://x.test/", SETTINGS, {})
	return soup


def style_of(el):
	return dict(p.split(":", 1) for p in (el.get("style") or "").split(";") if p)


class SpecificityTests(unittest.TestCase):
	def test_values(self):
		self.assertEqual(L.specificity("#a .b"), (1, 1, 0))
		self.assertEqual(L.specificity("div.a > p:hover"), (0, 2, 2))
		self.assertEqual(L.specificity('a[href^="x"]'), (0, 1, 1))
		self.assertEqual(L.specificity("*"), (0, 0, 0))


class ParseTests(unittest.TestCase):
	def test_flex_shorthand(self):
		self.assertEqual(dict(L._expand("flex", "1")), {"flex-grow": "1", "flex-basis": "0"})
		self.assertEqual(dict(L._expand("flex", "0 0 200px")), {"flex-grow": "0", "flex-basis": "200px"})
		self.assertEqual(dict(L._expand("flex", "none"))["flex-grow"], "0")
		self.assertEqual(dict(L._expand("flex", "auto"))["flex-basis"], "auto")

	def test_gap_shorthand(self):
		self.assertEqual(dict(L._expand("gap", "8px 16px")), {"row-gap": "8px", "column-gap": "16px"})
		self.assertEqual(dict(L._expand("gap", "8px")), {"row-gap": "8px", "column-gap": "8px"})

	def test_flex_flow(self):
		self.assertEqual(dict(L._expand("flex-flow", "column wrap")), {"flex-direction": "column", "flex-wrap": "wrap"})

	def test_tracks(self):
		self.assertEqual(L.parse_tracks("repeat(3, 1fr)", 900, 0), [("fr", 1)] * 3)
		self.assertEqual(L.parse_tracks("200px 1fr 2fr", 900, 0), [("px", 200), ("fr", 1), ("fr", 2)])
		self.assertEqual(len(L.parse_tracks("repeat(auto-fill, minmax(200px, 1fr))", 900, 10)), 4)
		self.assertEqual(L.parse_tracks("repeat(auto-fit, minmax(100px, 1fr))", 1000, 0), [("fr", 1)] * 10)
		self.assertEqual(L.parse_tracks("[a] 1fr [b] 100px", 900, 0), [("fr", 1), ("px", 100)])

	def test_px_value(self):
		self.assertEqual(L.px_value("12px"), 12)
		self.assertEqual(L.px_value("0"), 0)
		self.assertIsNone(L.px_value("50%"))


class FlexRowTests(unittest.TestCase):
	def test_row_becomes_table(self):
		s = run(".r{display:flex;gap:10px} .r>div{flex:1}", '<div class="r"><div id="a">A</div><div id="b">B</div></div>')
		r = s.select_one(".r")
		self.assertEqual(style_of(r)["display"], "table")
		self.assertEqual(style_of(r)["width"], "102%")  # widened by the gap, pulled back by the negative margins
		self.assertEqual(style_of(r)["margin-left"], "-10px")
		self.assertEqual(style_of(r)["margin-right"], "-10px")
		self.assertEqual(style_of(r)["border-spacing"], "10px 0")
		self.assertEqual(style_of(s.select_one("#a"))["display"], "table-cell")
		self.assertEqual(style_of(s.select_one("#a"))["width"], "50%")

	def test_fixed_sidebar_and_flexible_main(self):
		s = run(".r{display:flex} .side{flex:0 0 200px} .main{flex:1}",
				'<div class="r"><div class="side">S</div><div class="main">M</div></div>')
		self.assertEqual(style_of(s.select_one(".side"))["width"], "200px")
		self.assertNotIn("width", style_of(s.select_one(".main")))

	def test_grow_weights(self):
		s = run(".r{display:flex} #a{flex:2} #b{flex:1}", '<div class="r"><div id="a"></div><div id="b"></div></div>')
		self.assertEqual(style_of(s.select_one("#a"))["width"], "66.667%")

	def test_space_between_aligns_ends(self):
		s = run(".n{display:flex;justify-content:space-between;align-items:center}",
				'<div class="n"><div id="l">L</div><div id="r">R</div></div>')
		self.assertEqual(style_of(s.select_one("#l"))["text-align"], "left")
		self.assertEqual(style_of(s.select_one("#r"))["text-align"], "right")
		self.assertEqual(style_of(s.select_one("#l"))["vertical-align"], "middle")

	def test_center_shrinks_and_centres(self):
		s = run(".c{display:flex;justify-content:center}", '<div class="c"><span>x</span></div>')
		st = style_of(s.select_one(".c"))
		self.assertEqual(st["margin-left"], "auto")
		self.assertEqual(st["margin-right"], "auto")
		self.assertNotIn("width", st)

	def test_max_width_is_pinned(self):
		s = run(".c{display:flex;max-width:900px;margin:0 auto}", '<div class="c"><span>x</span></div>')
		self.assertEqual(style_of(s.select_one(".c"))["width"], "900px")

	def test_inline_flex(self):
		s = run(".c{display:inline-flex}", '<div class="c"><span>x</span></div>')
		self.assertEqual(style_of(s.select_one(".c"))["display"], "inline-table")

	def test_hidden_and_absolute_children_ignored(self):
		s = run(".r{display:flex} .h{display:none} .p{position:absolute} .r>div{flex:1}",
				'<div class="r"><div id="a"></div><div class="h"></div><div class="p"></div><div id="b"></div></div>')
		self.assertEqual(style_of(s.select_one("#a"))["width"], "50%")
		self.assertNotIn("style", s.select_one(".h").attrs)
		self.assertNotIn("style", s.select_one(".p").attrs)

	def test_reverse_falls_back_to_block(self):
		s = run(".r{display:flex;flex-direction:row-reverse}", '<div class="r"><div></div><div></div></div>')
		self.assertEqual(style_of(s.select_one(".r"))["display"], "block")
		self.assertNotIn("style", s.select_one(".r > div").attrs)


class SpacingCompensationTests(unittest.TestCase):
	def test_site_margins_are_preserved_in_the_compensation(self):
		s = run(".r{display:flex;gap:10px;margin:0 8px 20px}", '<div class="r"><i></i><i></i></div>')
		st = style_of(s.select_one(".r"))
		self.assertEqual(st["margin-left"], "-2px")
		self.assertEqual(st["margin-right"], "-2px")

	def test_auto_margins_are_left_alone(self):
		s = run(".r{display:flex;gap:10px;max-width:800px;margin:0 auto}", '<div class="r"><i></i><i></i></div>')
		st = style_of(s.select_one(".r"))
		self.assertNotIn("margin-left", st)
		self.assertEqual(st["width"], "800px")

	def test_grid_vertical_compensation(self):
		s = run(".g{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}", '<div class="g"><p></p><p></p></div>')
		st = style_of(s.select_one(".g"))
		self.assertEqual(st["margin-top"], "-16px")
		self.assertEqual(st["margin-bottom"], "4px")

	def test_wrap_trailing_margin_compensated(self):
		s = run(".w{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}", '<div class="w"><i></i></div>')
		st = style_of(s.select_one(".w"))
		self.assertEqual(st["margin-top"], "-8px")
		self.assertNotIn("margin-bottom", st)
		self.assertEqual(st["margin-right"], "-8px")
		self.assertNotIn("margin-left", st)
		self.assertEqual(style_of(s.select_one(".w i"))["margin-top"], "8px")

	def test_padding_reduces_table_width(self):
		s = run(".r{display:flex;padding:0 20px}", '<div class="r"><i></i></div>')
		self.assertEqual(style_of(s.select_one(".r"))["width"], "96%")

	def test_table_box_sizing_is_pinned(self):
		s = run(".r{display:flex}", '<div class="r"><i></i></div>')
		self.assertEqual(style_of(s.select_one(".r"))["box-sizing"], "content-box")

	def test_border_box_max_width_subtracts_padding(self):
		s = run("*{box-sizing:border-box} .r{display:flex;max-width:900px;padding:0 16px}", '<div class="r"><i></i></div>')
		self.assertEqual(style_of(s.select_one(".r"))["width"], "868px")

	def test_inherited_box_sizing(self):
		s = run("html{box-sizing:border-box} *{box-sizing:inherit} .r{display:flex;max-width:900px;padding:0 16px}", '<div class="r"><i></i></div>')
		self.assertEqual(style_of(s.select_one(".r"))["width"], "868px")

	def test_shrink_wrapped_nested_container_aligns_with_its_cell(self):
		s = run(".n{display:flex;justify-content:space-between} .l{display:flex;gap:6px}",
				'<div class="n"><b>logo</b><div class="l" id="l"><a>1</a><a>2</a></div></div>')
		inner = s.select_one("#l").find("div", recursive=False)
		st = style_of(inner)
		self.assertEqual(st["margin-left"], "auto")
		self.assertNotIn("width", st)


class FlexWrapColumnTests(unittest.TestCase):
	def test_wrap_floats_and_clears(self):
		s = run(".w{display:flex;flex-wrap:wrap;gap:8px}", '<div class="w"><i>a</i><i>b</i></div>')
		self.assertEqual(style_of(s.select_one(".w i"))["float"], "left")
		self.assertEqual(style_of(s.select_one(".w i"))["margin-right"], "8px")
		self.assertNotIn("margin-bottom", style_of(s.select_one(".w i")))
		self.assertIn("clear:both", s.select(".w > div")[-1]["style"])

	def test_column_gap_becomes_margins(self):
		s = run(".c{display:flex;flex-direction:column;gap:8px}", '<div class="c"><p id="a"></p><p id="b"></p><p id="c"></p></div>')
		self.assertEqual(style_of(s.select_one(".c"))["display"], "block")
		self.assertNotIn("style", s.select_one("#a").attrs)
		self.assertEqual(style_of(s.select_one("#b"))["margin-top"], "8px")

	def test_column_centered_items(self):
		s = run(".c{display:flex;flex-direction:column;align-items:center}", '<div class="c"><p id="a"></p></div>')
		st = style_of(s.select_one("#a"))
		self.assertEqual(st["display"], "table")
		self.assertEqual(st["margin-left"], "auto")

	def test_flex_flow_shorthand(self):
		s = run(".w{display:flex;flex-flow:row wrap}", '<div class="w"><i>a</i></div>')
		self.assertEqual(style_of(s.select_one(".w i"))["float"], "left")


class GridTests(unittest.TestCase):
	def test_three_columns(self):
		s = run(".g{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}", '<div class="g">' + "".join(f"<p>{i}</p>" for i in range(5)) + "</div>")
		g = s.select_one(".g")
		self.assertEqual(style_of(g)["display"], "table")
		self.assertEqual(style_of(g)["border-spacing"], "10px 10px")
		rows = [c for c in g.children if getattr(c, "name", None)]
		self.assertEqual(len(rows), 2)
		self.assertEqual([len(r.find_all("p", recursive=False)) for r in rows], [3, 2])
		self.assertEqual(style_of(rows[0])["display"], "table-row")
		cell = rows[0].find("p")
		self.assertEqual(style_of(cell)["display"], "table-cell")
		self.assertTrue(style_of(cell)["width"].endswith("%"))

	def test_fixed_and_fraction_columns(self):
		s = run(".g{display:grid;grid-template-columns:200px 1fr}", '<div class="g"><p id="a"></p><p id="b"></p></div>')
		self.assertEqual(style_of(s.select_one("#a"))["width"], "200px")

	def test_auto_fill(self):
		s = run(".g{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}",
				'<div class="g">' + "<p></p>" * 7 + "</div>")
		rows = [c for c in s.select_one(".g").children if getattr(c, "name", None)]
		self.assertEqual(len(rows[0].find_all("p", recursive=False)), 4)

	def test_single_column_stacks(self):
		s = run(".g{display:grid;gap:6px}", '<div class="g"><p id="a"></p><p id="b"></p></div>')
		self.assertEqual(style_of(s.select_one(".g"))["display"], "block")
		self.assertEqual(style_of(s.select_one("#b"))["margin-top"], "6px")

	def test_placement_falls_back_to_stacking(self):
		s = run(".g{display:grid;grid-template-columns:1fr 1fr} .w{grid-column:span 2}", '<div class="g"><p class="w"></p><p></p></div>')
		self.assertEqual(style_of(s.select_one(".g"))["display"], "block")

	def test_align_items_center(self):
		s = run(".g{display:grid;grid-template-columns:1fr 1fr;align-items:center}", '<div class="g"><p id="a"></p><p></p></div>')
		self.assertEqual(style_of(s.select_one("#a"))["vertical-align"], "middle")


class CascadeTests(unittest.TestCase):
	def test_specificity_wins_over_order(self):
		s = run("#r{display:block} .r{display:flex}", '<div id="r" class="r"><i></i></div>')
		self.assertNotIn("table", (s.select_one("#r").get("style") or ""))

	def test_later_rule_wins(self):
		s = run(".r{display:block} .r{display:flex}", '<div class="r"><i></i></div>')
		self.assertEqual(style_of(s.select_one(".r"))["display"], "table")

	def test_media_query_applies_at_viewport(self):
		s = run("@media (max-width:600px){.r{display:flex}} .r{display:block}", '<div class="r"><i></i></div>')
		self.assertNotIn("style", s.select_one(".r").attrs)
		s = run("@media (min-width:600px){.r{display:flex}}", '<div class="r"><i></i></div>')
		self.assertEqual(style_of(s.select_one(".r"))["display"], "table")

	def test_inline_style_makes_container(self):
		s = run("", '<div id="r" style="display:flex"><i></i><i></i></div>')
		self.assertEqual(style_of(s.select_one("#r"))["display"].split(";")[-1], "table")

	def test_vars_resolve(self):
		s = run(":root{--g:12px} .r{display:flex;gap:var(--g)}", '<div class="r"><i></i><i></i></div>')
		self.assertEqual(style_of(s.select_one(".r"))["border-spacing"], "12px 0")

	def test_nested_container_that_is_also_an_item(self):
		s = run(".o{display:flex} .o>div{flex:1} .i{display:flex;gap:4px}",
				'<div class="o"><div class="i" id="x"><b>1</b><b>2</b></div><div>other</div></div>')
		x = s.select_one("#x")
		self.assertEqual(style_of(x)["display"], "table-cell")
		inner = x.find("div", recursive=False)
		self.assertEqual(style_of(inner)["display"], "table")
		self.assertEqual(style_of(inner.find("b"))["display"], "table-cell")


class IndexTests(unittest.TestCase):
	"""The class/id/tag index must find exactly what a full select() finds, only faster."""

	HTML = ('<div id="top" class="a b"><ul class="list"><li class="item x">1</li><li class="item">2</li></ul>'
			'<p class="md:flex w-1/2">t</p><span data-k="v" class="a">s</span><div class="a"><b class="b">z</b></div></div>')

	def setUp(self):
		self.soup = BeautifulSoup(self.HTML, "html5lib")
		self.index = L.DocIndex(self.soup.find_all(True))

	def check(self, selector):
		found = {id(e) for e in self.index.candidates(selector) if soupsieve.match(selector, e)}
		expected = {id(e) for e in self.soup.select(selector)}
		self.assertEqual(found, expected, selector)

	def test_equivalent_to_select(self):
		for sel in ["div", ".a", ".a.b", "#top", "#top .item", "ul > li.item", "li:not(.x)", "li:nth-child(2)",
					"span[data-k]", ".missing", "div .missing .b", "*", ".a > .b", "p", ".list li.x", "b.b",
					".md\\:flex", ".w-1\\/2", "li.item + li", ":is(.a, .b)"]:
			self.check(sel)

	def test_missing_class_is_pruned_without_scanning(self):
		self.assertEqual(self.index.candidates(".nope .item"), [])

	def test_not_does_not_require_its_class(self):
		self.check("li:not(.absent)")


class RuleCacheTests(unittest.TestCase):
	def test_inline_sheets_with_the_same_base_do_not_share_cache_entries(self):
		pad = "/* " + "x" * 2100 + " */"
		a = (pad + ".one{display:flex}", "http://x.test/")
		b = (pad + ".two{display:grid}", "http://x.test/")
		rules = L.build_rules([a, b], SETTINGS, {})
		self.assertEqual(sorted(r.selector for r in rules), [".one", ".two"])
		# and again, now served from the cache
		rules = L.build_rules([a, b], SETTINGS, {})
		self.assertEqual(sorted(r.selector for r in rules), [".one", ".two"])
		self.assertEqual([r.order for r in rules], [1, 2])


class RobustnessTests(unittest.TestCase):
	def test_no_layout_is_a_noop(self):
		s = run("p{color:red}", "<p>hi</p>")
		self.assertNotIn("style", s.p.attrs)

	def test_bad_selectors_do_not_raise(self):
		run("a::before{display:flex} :bogus(){display:flex} .r{display:flex}", '<div class="r"><i></i></div>')

	def test_empty_container(self):
		s = run(".r{display:flex}", '<div class="r"></div>')
		self.assertEqual(style_of(s.select_one(".r"))["display"], "block")


if __name__ == "__main__":
	unittest.main()
