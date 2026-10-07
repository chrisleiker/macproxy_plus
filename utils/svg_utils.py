# Standard library imports
import colorsys
import re

# Third-party imports
from bs4 import Tag

"""
Prepare an inline <svg> to be turned into a picture, so the picture looks like the SVG did on the page.

The renderer that draws SVGs (Skia, in utils/image_utils.py) knows nothing about the page's CSS. Drawn on its own,
an icon is black and as big as its internal coordinate system (an Ars Technica icon whose page CSS says
"h-5 w-5 text-gray-300" would come out 40x40 and black, the logo 436x144 instead of 197x65). Here we work out what the
page's CSS makes of each SVG, using the same style cascade as the layout emulation, and write the answer onto the SVG:

  - display: none (a "hidden" class, a media query, ...)  -> the SVG is dropped
  - size: CSS width/height (px, em, rem), then the width/height attributes, then the viewBox
  - colour: the CSS `color` (what fill="currentColor" means), and any fill/stroke/opacity set by CSS
  - the picture is drawn at twice its displayed size, with transparency kept, and the <img> carries the SVG's class,
    id and style, so the site's CSS keeps positioning and sizing it (see the selector rewrite in utils/css_utils.py)
"""

SUPERSAMPLE = 2
MAX_RASTER = 1024
DEFAULT_FONT_PX = 16.0
PAINT_PROPS = ("fill", "stroke", "stroke-width", "stroke-opacity", "fill-opacity", "opacity", "stroke-linecap", "stroke-linejoin",
			   "fill-rule", "clip-rule", "stroke-dasharray", "stroke-miterlimit", "visibility")
COLOR_PROPS = ("fill", "stroke")
NAMED_PASS_THROUGH = {"none", "currentcolor", "transparent", "inherit", "context-fill", "context-stroke"}
SVG_NS = "http://www.w3.org/2000/svg"


# ----------------------------------------------------------------------------------
# Values
# ----------------------------------------------------------------------------------

def parse_color(value):
	"""'rgb(1 2 3 / .5)', 'hsl(...)', '#rgb', '#rrggbbaa' or a colour name -> (css colour, alpha 0..1), or None.

	Names are returned unchanged. Hex and functional notations become #rrggbb, with the alpha separated out, because
	the SVG renderer understands fill-opacity but not every colour syntax."""
	v = (value or "").strip()
	low = v.lower()
	if not v:
		return None
	if low in NAMED_PASS_THROUGH:
		return low if low != "currentcolor" else "currentColor", 1.0
	m = re.fullmatch(r"#([0-9a-f]{3,8})", low)
	if m:
		h = m.group(1)
		if len(h) in (3, 4):
			h = "".join(c * 2 for c in h)
		if len(h) == 6:
			return "#" + h, 1.0
		if len(h) == 8:
			return "#" + h[:6], int(h[6:], 16) / 255.0
		return None
	m = re.fullmatch(r"(rgba?|hsla?)\(\s*(.*?)\s*\)", low)
	if m:
		kind, body = m.groups()
		alpha = 1.0
		if "/" in body:
			body, _, a = body.partition("/")
			alpha = _alpha(a.strip())
		parts = [p for p in re.split(r"[\s,]+", body.strip()) if p]
		if len(parts) == 4 and "/" not in value:
			alpha = _alpha(parts.pop())
		if len(parts) != 3:
			return None
		try:
			if kind.startswith("rgb"):
				rgb = [_channel(p) for p in parts]
			else:
				h = float(re.sub(r"(deg|turn|rad|grad)$", "", parts[0]))
				if parts[0].endswith("turn"):
					h *= 360
				s, l = (float(p.rstrip("%")) / 100.0 for p in parts[1:])
				rgb = [c * 255 for c in colorsys.hls_to_rgb((h % 360) / 360.0, max(0, min(1, l)), max(0, min(1, s)))]
		except ValueError:
			return None
		return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb), alpha
	if re.fullmatch(r"[a-z]+", low):
		return low, 1.0
	if low.startswith("url("):
		return v, 1.0  # a gradient or pattern: keep exactly as written (ids are case-sensitive)
	return None


def _channel(text):
	return float(text[:-1]) * 2.55 if text.endswith("%") else float(text)


def _alpha(text):
	try:
		a = float(text[:-1]) / 100.0 if text.endswith("%") else float(text)
	except ValueError:
		return 1.0
	return max(0.0, min(1.0, a))


def length_px(value, font_px=DEFAULT_FONT_PX):
	"""'20px', '1.25rem', '2em', '12' -> pixels, or None for %, auto, calc() and anything else we cannot know."""
	m = re.fullmatch(r"\s*(-?[\d.]+)\s*(px|em|rem|pt|)\s*", str(value or ""), re.I)
	if not m:
		return None
	n, unit = float(m.group(1)), m.group(2).lower()
	if unit == "em":
		return n * font_px
	if unit == "rem":
		return n * DEFAULT_FONT_PX
	if unit == "pt":
		return n * 96 / 72
	return n


def parse_viewbox(value):
	try:
		nums = [float(x) for x in re.split(r"[\s,]+", (value or "").strip()) if x]
	except ValueError:
		return None
	return tuple(nums) if len(nums) == 4 and nums[2] > 0 and nums[3] > 0 else None


# ----------------------------------------------------------------------------------
# Working out what the page's CSS makes of one SVG
# ----------------------------------------------------------------------------------

def elements_to_style(svg_tags):
	"""Every element whose style we need to compute: each SVG, what is inside it, and its ancestors (for inherited values)."""
	seen = {}
	for svg in svg_tags:
		for el in [svg] + svg.find_all(True):
			seen[id(el)] = el
		for parent in svg.parents:
			if isinstance(parent, Tag):
				seen[id(parent)] = parent
	return list(seen.values())


def font_size_px(tag, cascade):
	"""The element's font size, following em/% sizes down from the root."""
	chain = [tag] + [p for p in tag.parents if isinstance(p, Tag)]
	size = DEFAULT_FONT_PX
	for el in reversed(chain):
		fs = cascade.props(el).get("font-size")
		if not fs:
			continue
		fs = fs.strip().lower()
		if fs.endswith("%"):
			try:
				size = size * float(fs[:-1]) / 100.0
			except ValueError:
				pass
		else:
			px = length_px(fs, size)
			if px:
				size = px
	return size


def current_color(tag, cascade):
	"""What `currentColor` is for this SVG: the nearest CSS colour on it or an ancestor, else the color attribute, else black."""
	for el in [tag] + [p for p in tag.parents if isinstance(p, Tag)]:
		value = (cascade.props(el).get("color") or "").strip()
		if value and value.lower() not in ("inherit", "currentcolor", "initial", "unset"):
			parsed = parse_color(value)
			if parsed:
				return parsed
	attr = parse_color(tag.get("color") or "")
	return attr or ("#000000", 1.0)


def display_size(tag, cascade):
	"""(width, height) in pixels the SVG is shown at, and its viewBox, or None if nothing tells us."""
	props = cascade.props(tag)
	fs = font_size_px(tag, cascade)
	vb = parse_viewbox(tag.get("viewBox") or tag.get("viewbox"))
	w = length_px(props.get("width"), fs)
	h = length_px(props.get("height"), fs)
	if w is None:
		w = length_px(tag.get("width"), fs)
	if h is None:
		h = length_px(tag.get("height"), fs)
	if vb:
		ratio = vb[3] / vb[2]
		if w is not None and h is None:
			h = w * ratio
		elif h is not None and w is None:
			w = h / ratio
		elif w is None and h is None:
			w, h = vb[2], vb[3]
	if (w is not None and w <= 0) or (h is not None and h <= 0):
		return (0, 0), vb
	if w is None or h is None:
		return None, vb
	max_w = length_px(props.get("max-width"), fs)
	if max_w is not None and w > max_w > 0:
		h, w = h * max_w / w, max_w
	return (w, h), vb


DRAWING = {"path", "circle", "rect", "ellipse", "line", "polyline", "polygon", "text", "image", "use", "foreignobject"}
NOT_DRAWN_INSIDE = {"defs", "symbol", "clippath", "mask", "pattern", "marker", "lineargradient", "radialgradient", "filter", "style"}


def has_visible_content(tag):
	"""False for a sprite sheet: an SVG that only holds <symbol>/<defs> definitions for other SVGs to use."""
	for el in tag.find_all(True):
		if el.name.lower() not in DRAWING:
			continue
		if any(isinstance(p, Tag) and p is not tag and p.name.lower() in NOT_DRAWN_INSIDE for p in el.parents):
			continue
		return True
	return False


class PreparedSvg:
	def __init__(self, markup, width, height, attrs, raster=None):
		self.markup = markup      # the SVG, ready to be drawn
		self.width = width        # displayed size in pixels (what the <img> should say)
		self.height = height
		self.attrs = attrs        # attributes to carry over to the <img>
		self.raster = raster      # (width, height) to draw it at, in pixels (None: let the renderer decide)


def prepare(tag, cascade):
	"""Return a PreparedSvg for an inline SVG, or None if the page's CSS hides it (the caller should drop it)."""
	props = cascade.props(tag)
	if (props.get("display") or "").strip().lower() == "none" or (props.get("visibility") or "").strip().lower() == "hidden":
		return None
	if not has_visible_content(tag):
		return None  # a sprite sheet of definitions, not a picture
	for parent in tag.parents:
		if isinstance(parent, Tag) and (cascade.props(parent).get("display") or "").strip().lower() == "none":
			return None  # inside something hidden: the <img> would be hidden too, but there is no point drawing it

	color, _ = current_color(tag, cascade)
	size, vb = display_size(tag, cascade)
	if size is not None and (size[0] <= 0 or size[1] <= 0):
		return None  # explicitly zero-sized (the usual way to hide a sprite sheet)
	original = {k: tag.get(k) for k in ("class", "id", "style", "title", "aria-label")}

	# Write the CSS-computed paint onto every element, since the renderer cannot read the page's stylesheets
	for el in [tag] + tag.find_all(True):
		ep = cascade.props(el)
		for prop in PAINT_PROPS:
			value = ep.get(prop)
			if value is None or value.strip().lower() in ("inherit", "initial", "unset", ""):
				continue
			value = value.strip()
			if prop in COLOR_PROPS:
				parsed = parse_color(value)
				if not parsed:
					continue
				el[prop] = "none" if parsed[0] == "transparent" else parsed[0]
				if parsed[1] < 1.0 and parsed[0] not in ("none", "transparent"):
					el[f"{prop}-opacity"] = f"{parsed[1]:g}"
			elif prop == "visibility":
				if value.lower() == "hidden":
					el["display"] = "none"
			else:
				el[prop] = value
		if (ep.get("display") or "").strip().lower() == "none" and el is not tag:
			el["display"] = "none"
		for attr in ("class", "style"):
			if el is not tag and attr in el.attrs:
				del el.attrs[attr]  # already baked in; the renderer would only misread them

	root = tag
	root["color"] = color  # the alpha of a CSS colour is ignored here: it must not dim parts that have their own fill
	if "xmlns" not in root.attrs:
		root["xmlns"] = SVG_NS
	if vb is None and size is not None:
		root["viewBox"] = f"0 0 {size[0]:g} {size[1]:g}"
	elif vb is not None:
		root["viewBox"] = " ".join(f"{n:g}" for n in vb)
	for attr in ("class", "style"):
		root.attrs.pop(attr, None)

	if size is None:
		return PreparedSvg(str(root), None, None, original)
	width, height = max(1, int(round(size[0]))), max(1, int(round(size[1])))
	scale = min(SUPERSAMPLE, MAX_RASTER / max(width, height)) if max(width, height) * SUPERSAMPLE > MAX_RASTER else SUPERSAMPLE
	raster = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
	root["width"] = str(raster[0])
	root["height"] = str(raster[1])
	return PreparedSvg(str(root), width, height, original, raster)
