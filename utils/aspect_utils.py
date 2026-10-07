# Standard library imports
import re

# Third-party imports
from bs4 import NavigableString, Tag
from bs4.element import CData, Comment, Declaration, Doctype, ProcessingInstruction

# First-party imports
from utils.layout_utils import _add_style

"""
Emulate CSS `aspect-ratio` for browsers that do not have it.

Sites make a 16:9 picture box with `aspect-ratio: 16/9` on a container whose children are all absolutely positioned
(Tailwind's `aspect-video`, `aspect-square`, `aspect-[4/3]`). Without the property such a box has no height at all, so
the pictures inside it vanish, or, with min-width/min-height 100% on them, stretch across the whole page.

Percentage padding is relative to an element's own width in every CSS engine, so a box that should be W:H tall can be
`height:0; padding-bottom: H/W*100%`, exact at any width. That is only safe when nothing inside the box takes up space
by itself (the children are out of flow), so:

  - a block whose content is all out of flow (or empty)       -> that padding is put on the block itself
  - the same, when it is a table cell / inline-block (emulated flex or grid item, where the padding would be measured
    against the wrong width)                                    -> the same box is made inside it instead
  - an <iframe>, <video>, <canvas>, <embed>, <object> with no height of its own -> wrapped in such a box
  - anything with in-flow content (text, a normal image) or a height of its own is left alone: its content gives it a
    sensible height already
"""

REPLACED = {"iframe", "video", "canvas", "embed", "object"}
NEVER = {"img", "svg", "picture", "source", "html", "head", "body", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
		 "input", "button", "select", "textarea", "script", "style", "link", "meta"}
INLINE_TAGS = {"a", "span", "b", "i", "em", "strong", "small", "abbr", "code", "label", "u", "s", "mark", "sub", "sup", "cite",
			   "q", "time", "font", "big"}
OUT_OF_FLOW = ("absolute", "fixed")
RATIO = re.compile(r"\s*(?:auto\s+)?(\d+(?:\.\d+)?)\s*(?:/\s*(\d+(?:\.\d+)?))?\s*")


def parse_ratio(value):
	"""'16 / 9', '16/9', '1', 'auto 4/3' -> (16.0, 9.0) ...; plain 'auto' and anything unusable -> None."""
	m = RATIO.fullmatch((value or "").lower())
	if not m:
		return None
	w, h = float(m.group(1)), float(m.group(2) or 1)
	return (w, h) if w > 0 and h > 0 else None


def enabled(config):
	"""Only for presets whose browser lacks `aspect-ratio` (it is in CSS_UNSUPPORTED_PROPERTIES) and that translate CSS."""
	if not getattr(config, "ASPECT_RATIO_EMULATION", True):
		return False
	if getattr(config, "CSS_MODE", "strip") != "downlevel":
		return False
	return "aspect-ratio" in {str(p).lower() for p in (getattr(config, "CSS_UNSUPPORTED_PROPERTIES", None) or ())}


def _value(props, name):
	return (props.get(name) or "").strip().lower()


def _is_inline(el, props):
	display = _value(props, "display")
	return display == "inline" or (not display and el.name.lower() in INLINE_TAGS)


def has_in_flow_content(el, cascade):
	"""Does anything inside `el` take up space by itself? Text and normal boxes do; absolutely positioned boxes do not, and an
	inline wrapper (an <a> around an absolutely positioned picture) is only as tall as what is inside it."""
	for child in el.children:
		if isinstance(child, NavigableString):
			if not isinstance(child, (Comment, Doctype, Declaration, ProcessingInstruction, CData)) and str(child).strip():
				return True
			continue
		if not isinstance(child, Tag) or child.name.lower() in ("script", "style", "link", "meta", "template", "noscript"):
			continue
		props = cascade.props(child)
		if _value(props, "display") == "none" or _value(props, "position") in OUT_OF_FLOW:
			continue
		if child.name.lower() in REPLACED | {"img", "svg", "picture", "input", "button", "select", "textarea"}:
			return True
		if _is_inline(child, props):
			if has_in_flow_content(child, cascade):
				return True
			continue
		return True  # a block-level box: assume it has height
	return False


def _definite_height(el, props):
	height = _value(props, "height")
	if height and height not in ("auto", "initial", "unset", "inherit", "fit-content", "min-content", "max-content"):
		return True
	return False


def _padding(ratio):
	return f"{ratio[1] / ratio[0] * 100:.4f}".rstrip("0").rstrip(".") + "%"


def _is_cell_like(el, props):
	inline_style = (el.get("style") or "").replace(" ", "").lower()
	display = _value(props, "display")
	return "display:table-cell" in inline_style or display in ("table-cell", "inline-block", "inline-flex", "inline-grid", "table")


def emulate(soup, cascade, settings=None):
	"""Give aspect-ratio boxes a height. `cascade` is a layout_utils.StyleCascade built while the page's CSS was still
	present. Returns how many elements were changed."""
	candidates = cascade.elements_with("aspect-ratio", soup)
	if not candidates:
		return 0
	subtree = {}
	for el in candidates:
		for inner in [el] + el.find_all(True):
			subtree[id(inner)] = inner
	cascade.compute(subtree.values())

	changed = 0
	for el in candidates:
		if el.decomposed or el.name.lower() in NEVER:
			continue
		props = cascade.props(el)
		ratio = parse_ratio(props.get("aspect-ratio"))
		if ratio is None or _value(props, "display") == "none":
			continue
		tag = el.name.lower()
		pad = _padding(ratio)

		if tag in REPLACED:
			if _definite_height(el, props):
				continue
			wrapper_style = f"position:relative;height:0;padding-bottom:{pad}"
			width = _value(props, "width")
			if width.endswith("px"):
				wrapper_style += f";width:{width};max-width:100%"
			wrapper = soup.new_tag("div", style=wrapper_style)
			el.replace_with(wrapper)
			wrapper.append(el)
			_add_style(el, [("position", "absolute"), ("top", "0"), ("left", "0"), ("width", "100%"), ("height", "100%")])
			changed += 1
			continue

		if _definite_height(el, props) or has_in_flow_content(el, cascade):
			continue
		if _is_cell_like(el, props):
			wrapper = soup.new_tag("div", style=f"position:relative;height:0;padding-bottom:{pad}")
			for child in list(el.contents):
				wrapper.append(child.extract())
			el.append(wrapper)
		else:
			pairs = [("height", "0"), ("padding-bottom", pad)]
			if _is_inline(el, props):
				pairs.insert(0, ("display", "block"))
			_add_style(el, pairs)
		changed += 1
	return changed
