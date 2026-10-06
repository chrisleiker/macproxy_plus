# Standard library imports
import colorsys
import re
from dataclasses import dataclass, field

# Third-party imports
import requests
import tinycss2
from tinycss2 import ast as A

"""
Downlevel modern CSS so that older browsers can use it.

What it does, driven by a capability profile (see CSSSettings and the presets):
  - flattens @import and CSS nesting, unwraps @layer
  - evaluates @media / @supports against a fixed viewport and unwraps the result
  - resolves var(), calc()/min()/max()/clamp(), rem and viewport units
  - rewrites rgba()/hsl()/#rrggbbaa colors to opaque hex (blended against white)
  - replaces gradients with their first color stop
  - drops unsupported properties, values, selectors and at-rules
Anything that cannot be translated is dropped rather than passed through, so the
browser never sees syntax it would choke on.
"""

REM_PX = 16.0
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"
MAX_FETCH_BYTES = 2 * 1024 * 1024
ROOT_SELECTORS = {":root", "html", "body", "*"}

GRADIENT_FUNCTIONS = {
	"linear-gradient", "radial-gradient", "conic-gradient",
	"repeating-linear-gradient", "repeating-radial-gradient", "repeating-conic-gradient",
	"-webkit-linear-gradient", "-webkit-radial-gradient", "-moz-linear-gradient", "-moz-radial-gradient",
}
MODERN_COLOR_FUNCTIONS = {"hwb", "lab", "lch", "oklab", "oklch", "color", "color-mix", "light-dark"}
VIEWPORT_UNITS = {"vw", "vh", "vmin", "vmax"}
NEW_VIEWPORT_UNITS = {
	"dvw", "dvh", "dvmin", "dvmax", "svw", "svh", "svmin", "svmax",
	"lvw", "lvh", "lvmin", "lvmax", "vi", "vb",
}
ABSOLUTE_UNITS_PX = {"px": 1.0, "pt": 96 / 72, "pc": 16.0, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4, "q": 96 / 101.6}
LOGICAL_PROPERTIES = {
	"margin-inline-start": "margin-left", "margin-inline-end": "margin-right",
	"margin-block-start": "margin-top", "margin-block-end": "margin-bottom",
	"padding-inline-start": "padding-left", "padding-inline-end": "padding-right",
	"padding-block-start": "padding-top", "padding-block-end": "padding-bottom",
	"inset-inline-start": "left", "inset-inline-end": "right",
	"inset-block-start": "top", "inset-block-end": "bottom",
	"inline-size": "width", "block-size": "height",
	"min-inline-size": "min-width", "min-block-size": "min-height",
	"max-inline-size": "max-width", "max-block-size": "max-height",
}


@dataclass
class CSSSettings:
	viewport_width: int = 1024
	viewport_height: int = 768
	# Feature names: var, calc, minmax, color-functions, hexalpha, rem, viewport-units,
	# new-viewport-units, gradients, modern-color, double-colon, logical-properties, inset
	unsupported_features: frozenset = frozenset()
	unsupported_properties: frozenset = frozenset()
	unsupported_values: dict = field(default_factory=dict)
	unsupported_selectors: tuple = ()
	property_renames: dict = field(default_factory=dict)
	strip_at_rules: frozenset = frozenset({"font-face", "keyframes", "counter-style", "property", "namespace", "charset"})
	drop_vendor_prefixes: tuple = ("-webkit-", "-ms-", "-o-")
	max_import_depth: int = 3

	def __post_init__(self):
		self._selector_res = [re.compile(p, re.I) for p in self.unsupported_selectors]

	def selector_ok(self, selector):
		return not any(r.search(selector) for r in self._selector_res)


def settings_from_config(config):
	"""Build CSSSettings from the config module (after any preset has been applied)."""
	defaults = CSSSettings()
	g = lambda name, default: getattr(config, name, default)
	return CSSSettings(
		viewport_width=g("CSS_VIEWPORT_WIDTH", defaults.viewport_width),
		viewport_height=g("CSS_VIEWPORT_HEIGHT", defaults.viewport_height),
		unsupported_features=frozenset(g("CSS_UNSUPPORTED_FEATURES", ())),
		unsupported_properties=frozenset(p.lower() for p in g("CSS_UNSUPPORTED_PROPERTIES", ())),
		unsupported_values={k.lower(): {v.lower() for v in vs} for k, vs in g("CSS_UNSUPPORTED_VALUES", {}).items()},
		unsupported_selectors=tuple(g("CSS_UNSUPPORTED_SELECTORS", ())),
		property_renames={k.lower(): list(v) for k, v in g("CSS_PROPERTY_RENAMES", {}).items()},
		strip_at_rules=frozenset(g("CSS_STRIP_AT_RULES", defaults.strip_at_rules)),
		drop_vendor_prefixes=tuple(g("CSS_DROP_VENDOR_PREFIXES", defaults.drop_vendor_prefixes)),
		max_import_depth=g("CSS_MAX_IMPORT_DEPTH", defaults.max_import_depth),
	)


class Drop(Exception):
	"""Raised when a value cannot be translated and its declaration must be dropped."""


# ----------------------------------------------------------------------------------
# Fetching / decoding
# ----------------------------------------------------------------------------------

def fetch_text(url):
	"""Fetch a stylesheet for @import inlining. Returns text, or None on any failure."""
	try:
		resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10, stream=True)
		resp.raise_for_status()
		data = resp.raw.read(MAX_FETCH_BYTES + 1, decode_content=True)
		if len(data) > MAX_FETCH_BYTES:
			return None
		return decode_css(data, resp.headers.get("Content-Type", ""))
	except Exception as e:
		print(f"CSS @import fetch failed for {url}: {e}")
		return None


def decode_css(content, content_type=""):
	if not isinstance(content, bytes):
		return content
	m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
	for enc in ([m.group(1)] if m else []) + ["utf-8", "latin-1"]:
		try:
			return content.decode(enc).lstrip("﻿")
		except (UnicodeDecodeError, LookupError):
			continue
	return content.decode("utf-8", errors="replace")


# ----------------------------------------------------------------------------------
# Media queries and @supports
# ----------------------------------------------------------------------------------

def _length_px(text, vw, vh):
	m = re.fullmatch(r"\s*(-?[\d.]+)\s*([a-z%]*)\s*", text)
	if not m:
		return None
	n, unit = float(m.group(1)), m.group(2)
	if unit in ABSOLUTE_UNITS_PX:
		return n * ABSOLUTE_UNITS_PX[unit]
	if unit in ("em", "rem"):
		return n * REM_PX
	if unit == "" and n == 0:
		return 0.0
	return None


def _eval_media_feature(feature, vw, vh):
	feature = feature.strip().lower()
	# Range syntax: (width >= 600px), (400px <= width <= 800px)
	m = re.fullmatch(r"([\w.\-]+)\s*(<=|>=|<|>|=)\s*([\w.\-]+)", feature)
	m3 = re.fullmatch(r"([\w.\-]+)\s*(<=|<)\s*(width|height)\s*(<=|<)\s*([\w.\-]+)", feature)
	dims = {"width": vw, "height": vh}
	if m3:
		lo, op1, name, op2, hi = m3.groups()
		lo_px, hi_px = _length_px(lo, vw, vh), _length_px(hi, vw, vh)
		if lo_px is None or hi_px is None:
			return False
		v = dims[name]
		return (lo_px <= v if op1 == "<=" else lo_px < v) and (v <= hi_px if op2 == "<=" else v < hi_px)
	if m:
		a, op, b = m.groups()
		if a in dims:
			name, other, flip = a, _length_px(b, vw, vh), False
		elif b in dims:
			name, other, flip = b, _length_px(a, vw, vh), True
		else:
			return False
		if other is None:
			return False
		v = dims[name]
		if flip:
			op = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "=": "="}[op]
		return {"<": v < other, ">": v > other, "<=": v <= other, ">=": v >= other, "=": v == other}[op]

	if ":" not in feature:
		# Boolean-context features
		return feature == "color"
	name, _, value = [s.strip() for s in feature.partition(":")]
	if name in ("min-width", "max-width", "min-height", "max-height", "width", "height"):
		px = _length_px(value, vw, vh)
		if px is None:
			return False
		dim = vw if name.endswith("width") else vh
		if name.startswith("min-"):
			return dim >= px
		if name.startswith("max-"):
			return dim <= px
		return dim == px
	if name == "orientation":
		return value == ("landscape" if vw >= vh else "portrait")
	if name == "prefers-color-scheme":
		return value == "light"
	if name == "prefers-reduced-motion":
		return value == "no-preference"
	if name == "hover":
		return value == "hover"
	if name in ("pointer", "any-pointer"):
		return value == "fine"
	if name in ("any-hover",):
		return value == "hover"
	if name in ("resolution", "min-resolution", "-webkit-min-device-pixel-ratio", "min--moz-device-pixel-ratio"):
		mm = re.fullmatch(r"([\d.]+)\s*(dppx|x|dpi)?", value)
		if not mm:
			return False
		dppx = float(mm.group(1)) / (96 if mm.group(2) == "dpi" else 1)
		return dppx <= 1.0 if name.startswith(("min", "-webkit-min")) else dppx == 1.0
	if name in ("max-resolution", "-webkit-max-device-pixel-ratio"):
		return True
	if name == "color":
		return True
	if name == "display-mode":
		return value == "browser"
	return False  # unknown feature -> query doesn't match, per spec


def _split_top_level(text, sep):
	parts, depth, cur, i = [], 0, "", 0
	while i < len(text):
		ch = text[i]
		if ch == "(":
			depth += 1
		elif ch == ")":
			depth -= 1
		if depth == 0 and text.startswith(sep, i):
			parts.append(cur)
			cur = ""
			i += len(sep)
			continue
		cur += ch
		i += 1
	parts.append(cur)
	return parts


def _is_wrapped(text):
	"""True if the first '(' in text is closed by the final ')'."""
	if not (text.startswith("(") and text.endswith(")")):
		return False
	depth = 0
	for i, ch in enumerate(text):
		depth += (ch == "(") - (ch == ")")
		if depth == 0 and i < len(text) - 1:
			return False
	return True


def _eval_media_condition(cond, vw, vh):
	cond = cond.strip()
	if _is_wrapped(cond):
		inner = cond[1:-1].strip()
		if inner.startswith("(") or inner.startswith("not ") or re.search(r"\)\s+(and|or)\s+\(", inner):
			return _eval_media_condition(inner, vw, vh)
		return _eval_media_feature(inner, vw, vh)
	if cond.startswith("not "):
		return not _eval_media_condition(cond[4:], vw, vh)
	ors = _split_top_level(cond, " or ")
	if len(ors) > 1:
		return any(_eval_media_condition(p, vw, vh) for p in ors)
	ands = _split_top_level(cond, " and ")
	if len(ands) > 1:
		return all(_eval_media_condition(p, vw, vh) for p in ands)
	return _eval_media_feature(cond, vw, vh)


def _eval_media_query(query, vw, vh):
	q = query.strip().lower()
	if not q:
		return True
	negate = False
	if q.startswith("not "):
		negate, q = True, q[4:].strip()
	elif q.startswith("only "):
		q = q[5:].strip()
	m = re.match(r"([a-z]+)\b\s*(.*)", q)
	media_type, rest = "all", q
	if m and m.group(1) in ("all", "screen", "print", "speech", "tty", "tv", "projection", "handheld", "braille", "embossed", "aural"):
		media_type, rest = m.group(1), m.group(2)
		rest = re.sub(r"^and\s+", "", rest)
	result = media_type in ("all", "screen")
	if result and rest.strip():
		result = _eval_media_condition(rest, vw, vh)
	return result != negate


def media_matches(media, settings):
	"""Does a media query list (e.g. from @media or <link media>) match our fixed viewport?"""
	media = (media or "").strip()
	if not media:
		return True
	return any(_eval_media_query(q, settings.viewport_width, settings.viewport_height) for q in _split_top_level(media, ","))


def _supports_term(term, settings):
	term = term.strip()
	negate = False
	if term.startswith("not "):
		negate, term = True, term[4:].strip()
	if term.startswith("(") and term.endswith(")"):
		term = term[1:-1].strip()
	if term.startswith("selector("):
		return settings.selector_ok(term[9:-1]) != negate
	if term.startswith("(") or " and " in term or " or " in term:
		return _supports_condition(term, settings) != negate
	prop, _, value = term.partition(":")
	prop, value = prop.strip().lower(), value.strip().lower()
	ok = True
	if prop in settings.unsupported_properties or prop.startswith(settings.drop_vendor_prefixes):
		ok = False
	bad_values = settings.unsupported_values.get(prop, set())
	if value.split(" ")[0] in bad_values:
		ok = False
	return ok != negate


def _supports_condition(cond, settings):
	cond = cond.strip()
	ors = _split_top_level(cond, " or ")
	if len(ors) > 1:
		return any(_supports_condition(p, settings) for p in ors)
	ands = _split_top_level(cond, " and ")
	if len(ands) > 1:
		return all(_supports_condition(p, settings) for p in ands)
	return _supports_term(cond, settings)


# ----------------------------------------------------------------------------------
# Value translation
# ----------------------------------------------------------------------------------

@dataclass
class _Ctx:
	settings: CSSSettings
	vars: dict
	base_url: str = None
	fetch_cache: dict = field(default_factory=dict)

	def unsupported(self, feature):
		return feature in self.settings.unsupported_features


def _fmt(n):
	s = ("%.3f" % n).rstrip("0").rstrip(".")
	return "0" if s in ("", "-0") else s


def _to_px(value, unit, ctx):
	"""Convert a number+unit to px if the unit is one we are converting. Returns None if not."""
	unit = unit.lower()
	vw, vh = ctx.settings.viewport_width, ctx.settings.viewport_height
	if unit == "rem" and ctx.unsupported("rem"):
		return value * REM_PX
	if unit in VIEWPORT_UNITS and ctx.unsupported("viewport-units") or unit in NEW_VIEWPORT_UNITS and (
			ctx.unsupported("viewport-units") or ctx.unsupported("new-viewport-units")):
		table = {"w": vw, "h": vh, "min": min(vw, vh), "max": max(vw, vh)}
		key = "min" if unit.endswith("min") else "max" if unit.endswith("max") else unit[-1]
		if unit in ("vi",):
			key = "w"
		elif unit in ("vb",):
			key = "h"
		return value * table[key] / 100.0
	return None


def _significant(tokens):
	return [t for t in tokens if not isinstance(t, (A.WhitespaceToken, A.Comment))]


def _split_commas(tokens):
	groups, cur = [], []
	for t in tokens:
		if isinstance(t, A.LiteralToken) and t.value == ",":
			groups.append(cur)
			cur = []
		else:
			cur.append(t)
	groups.append(cur)
	return groups


def _reparse(tokens, ctx, depth):
	return tinycss2.parse_component_value_list(_convert(tokens, ctx, depth))


def _convert(tokens, ctx, depth=0):
	if depth > 12:
		raise Drop("recursion")
	return "".join(_convert_token(t, ctx, depth) for t in tokens)


def _convert_token(t, ctx, depth):
	if isinstance(t, A.DimensionToken):
		px = _to_px(t.value, t.unit, ctx)
		return f"{_fmt(px)}px" if px is not None else t.serialize()
	if isinstance(t, A.HashToken):
		if ctx.unsupported("hexalpha") and re.fullmatch(r"([0-9a-fA-F]{4}|[0-9a-fA-F]{8})", t.value):
			return _hex_alpha_to_hex(t.value)
		return t.serialize()
	if isinstance(t, A.FunctionBlock):
		name = t.lower_name
		if name == "var" and ctx.unsupported("var"):
			return _expand_var(t, ctx, depth)
		if name in ("calc", "-moz-calc", "-webkit-calc") and ctx.unsupported("calc"):
			return _eval_math(t, ctx, depth)
		if name in ("min", "max", "clamp") and ctx.unsupported("minmax"):
			return _eval_math(t, ctx, depth)
		if name in ("rgb", "rgba", "hsl", "hsla") and ctx.unsupported("color-functions"):
			return _color_function(t, ctx, depth)
		if name in MODERN_COLOR_FUNCTIONS and ctx.unsupported("modern-color"):
			raise Drop(name)
		if name in GRADIENT_FUNCTIONS and ctx.unsupported("gradients"):
			raise Drop(name)
		if name == "env":
			raise Drop(name)
		return f"{t.name}({_convert(t.arguments, ctx, depth + 1)})"
	if isinstance(t, A.ParenthesesBlock):
		return f"({_convert(t.content, ctx, depth + 1)})"
	if isinstance(t, A.SquareBracketsBlock):
		return f"[{_convert(t.content, ctx, depth + 1)}]"
	return t.serialize()


def _expand_var(t, ctx, depth):
	groups = _split_commas(t.arguments)
	first = _significant(groups[0])
	if not first or not isinstance(first[0], A.IdentToken) or not first[0].value.startswith("--"):
		raise Drop("bad var()")
	name = first[0].value
	if name in ctx.vars:
		return _convert(tinycss2.parse_component_value_list(ctx.vars[name]), ctx, depth + 1)
	if len(groups) > 1:
		fallback = []
		for i, g in enumerate(groups[1:]):
			if i:
				fallback.append(A.LiteralToken(0, 0, ","))
			fallback.extend(g)
		return _convert(fallback, ctx, depth + 1)
	raise Drop(f"undefined {name}")


# --- math ---

class _Math:
	"""Tiny evaluator for calc()/min()/max()/clamp() expressions. Values are (number, unit)."""

	def __init__(self, tokens, ctx, depth=0):
		self.toks = _significant(tokens)
		self.i = 0
		self.ctx = ctx
		self.depth = depth

	def peek(self):
		return self.toks[self.i] if self.i < len(self.toks) else None

	def take(self):
		t = self.peek()
		self.i += 1
		return t

	def expr(self):
		v = self.term()
		while True:
			t = self.peek()
			if isinstance(t, A.LiteralToken) and t.value in "+-":
				self.take()
				r = self.term()
				v = self._addsub(v, r, 1 if t.value == "+" else -1)
			else:
				return v

	def term(self):
		v = self.factor()
		while True:
			t = self.peek()
			if isinstance(t, A.LiteralToken) and t.value in "*/":
				self.take()
				r = self.factor()
				if t.value == "*":
					if v[1] and r[1]:
						raise Drop("unit*unit")
					v = (v[0] * r[0], v[1] or r[1])
				else:
					if r[1] or r[0] == 0:
						raise Drop("div")
					v = (v[0] / r[0], v[1])
			else:
				return v

	def factor(self):
		t = self.take()
		if t is None:
			raise Drop("eof")
		if isinstance(t, A.NumberToken):
			return (t.value, "")
		if isinstance(t, A.PercentageToken):
			return (t.value, "%")
		if isinstance(t, A.DimensionToken):
			unit = t.unit.lower()
			if unit in ABSOLUTE_UNITS_PX:
				return (t.value * ABSOLUTE_UNITS_PX[unit], "px")
			px = _to_px(t.value, unit, self.ctx)
			if px is not None:
				return (px, "px")
			if unit == "rem":
				return (t.value * REM_PX, "px")
			return (t.value, unit)
		if isinstance(t, A.ParenthesesBlock):
			return _Math(t.content, self.ctx, self.depth + 1).whole()
		if isinstance(t, A.FunctionBlock):
			return self.function(t)
		raise Drop("token")

	def function(self, t):
		name = t.lower_name
		if self.depth > 12:
			raise Drop("recursion")
		if name == "var":
			expanded = tinycss2.parse_component_value_list(_expand_var(t, self.ctx, self.depth + 1))
			return _Math(expanded, self.ctx, self.depth + 1).whole()
		if name in ("calc", "-moz-calc", "-webkit-calc"):
			return _Math(t.arguments, self.ctx, self.depth + 1).whole()
		if name in ("min", "max", "clamp"):
			args = [_Math(g, self.ctx, self.depth + 1).whole() for g in _split_commas(t.arguments)]
			units = {a[1] for a in args}
			if len(units) != 1:
				raise Drop("mixed units in " + name)
			nums = [a[0] for a in args]
			if name == "min":
				v = min(nums)
			elif name == "max":
				v = max(nums)
			else:
				if len(nums) != 3:
					raise Drop("clamp args")
				v = max(nums[0], min(nums[1], nums[2]))
			return (v, units.pop())
		raise Drop("function " + name)

	@staticmethod
	def _addsub(a, b, sign):
		if a[1] != b[1]:
			raise Drop("mixed units")
		return (a[0] + sign * b[0], a[1])

	def whole(self):
		v = self.expr()
		if self.peek() is not None:
			raise Drop("trailing")
		return v


def _eval_math(t, ctx, depth):
	value, unit = _Math([t], ctx, depth).whole()
	return f"{_fmt(value)}{unit}" if unit else _fmt(value)


# --- colors ---

def _blend(r, g, b, a):
	if a >= 1:
		return r, g, b
	return tuple(c * a + 255 * (1 - a) for c in (r, g, b))


def _hex(r, g, b):
	r, g, b = (max(0, min(255, int(round(c)))) for c in (r, g, b))
	return f"#{r:02x}{g:02x}{b:02x}"


def _hex_alpha_to_hex(h):
	if len(h) == 4:
		h = "".join(c * 2 for c in h)
	r, g, b, a = (int(h[i:i + 2], 16) for i in (0, 2, 4, 6))
	if a == 0:
		return "transparent"
	return _hex(*_blend(r, g, b, a / 255.0))


def _color_function(t, ctx, depth):
	name = t.lower_name
	args = _significant(_reparse(t.arguments, ctx, depth + 1))
	nums, alpha, after_slash = [], None, False
	for a in args:
		if isinstance(a, A.LiteralToken):
			if a.value == "/":
				after_slash = True
			continue  # commas
		if isinstance(a, A.IdentToken) and a.lower_value == "none":
			v = (0.0, "")
		elif isinstance(a, A.NumberToken):
			v = (a.value, "")
		elif isinstance(a, A.PercentageToken):
			v = (a.value, "%")
		elif isinstance(a, A.DimensionToken) and a.lower_unit in ("deg", "turn", "rad", "grad"):
			v = (a.value, a.lower_unit)
		else:
			raise Drop("color arg")
		if after_slash:
			alpha = v
		else:
			nums.append(v)
	if len(nums) == 4 and alpha is None:  # legacy rgba(r,g,b,a)
		alpha = nums.pop()
	if len(nums) != 3:
		raise Drop("color arity")
	a = 1.0
	if alpha is not None:
		a = alpha[0] / 100.0 if alpha[1] == "%" else alpha[0]
		a = max(0.0, min(1.0, a))
	if name.startswith("rgb"):
		rgb = [n[0] * 2.55 if n[1] == "%" else n[0] for n in nums]
	else:
		h, s, l = nums
		deg = {"": 1.0, "deg": 1.0, "turn": 360.0, "rad": 57.29578, "grad": 0.9}[h[1]] * h[0]
		sat = (s[0] if s[1] == "%" else s[0]) / 100.0
		lig = (l[0] if l[1] == "%" else l[0]) / 100.0
		rgb = [c * 255 for c in colorsys.hls_to_rgb((deg % 360) / 360.0, max(0, min(1, lig)), max(0, min(1, sat)))]
	if a == 0:
		return "transparent"
	return _hex(*_blend(*rgb, a))


# ----------------------------------------------------------------------------------
# Declarations
# ----------------------------------------------------------------------------------

def _first_color_in(tokens, ctx, depth):
	"""Find the first color-looking value inside a gradient's arguments."""
	for t in tokens:
		try:
			if isinstance(t, A.HashToken):
				return _convert([t], ctx, depth)
			if isinstance(t, A.FunctionBlock) and t.lower_name in ("rgb", "rgba", "hsl", "hsla"):
				return _convert([t], ctx, depth)
			if isinstance(t, A.IdentToken) and t.lower_value not in (
					"to", "top", "left", "right", "bottom", "center", "circle", "ellipse", "closest-side",
					"farthest-side", "closest-corner", "farthest-corner", "at", "in", "from"):
				return t.value
		except Drop:
			continue
	return None


def _process_gradients(decl, ctx):
	"""Strip gradient layers from background declarations; fall back to the first color stop."""
	layers = _split_commas(decl.value)
	kept, first_color = [], None
	for layer in layers:
		grad = next((t for t in layer if isinstance(t, A.FunctionBlock) and t.lower_name in GRADIENT_FUNCTIONS), None)
		if grad is None:
			kept.append(layer)
		elif first_color is None:
			first_color = _first_color_in(grad.arguments, ctx, 1)
	out = []
	if kept:
		try:
			value = ",".join(_convert(layer, ctx).strip() for layer in kept).strip()
		except Drop:
			value = ""
		if value:
			out.append((decl.name, value))
	if first_color:
		out.append(("background-color", first_color))
	return out


def _has_gradient(tokens):
	return any(isinstance(t, A.FunctionBlock) and t.lower_name in GRADIENT_FUNCTIONS for t in tokens)


def _expand_inset(value):
	parts = value.split()
	if not 1 <= len(parts) <= 4:
		return []
	top = parts[0]
	right = parts[1] if len(parts) > 1 else top
	bottom = parts[2] if len(parts) > 2 else top
	left = parts[3] if len(parts) > 3 else right
	return [("top", top), ("right", right), ("bottom", bottom), ("left", left)]


def _process_declaration(decl, ctx):
	"""Returns a list of (name, value, important) triples to emit."""
	s = ctx.settings
	name = decl.lower_name
	imp = decl.important

	if name.startswith("--"):
		if ctx.unsupported("var"):
			return []
		return [(decl.name, tinycss2.serialize(decl.value).strip(), imp)]
	if name.startswith(s.drop_vendor_prefixes):
		return []
	if name in LOGICAL_PROPERTIES and ctx.unsupported("logical-properties"):
		name = LOGICAL_PROPERTIES[name]
	if name == "inset" and ctx.unsupported("inset"):
		try:
			return [(n, v, imp) for n, v in _expand_inset(_convert(decl.value, ctx).strip())]
		except Drop:
			return []
	if name in s.unsupported_properties:
		return []

	if name in ("background", "background-image") and ctx.unsupported("gradients") and _has_gradient(decl.value):
		pairs = _process_gradients(decl, ctx)
		return [(n, v, imp) for n, v in pairs]
	try:
		value = _convert(decl.value, ctx).strip()
	except Drop:
		return []
	if not value:
		return []

	bad = s.unsupported_values.get(name)
	if bad and any(isinstance(t, A.IdentToken) and t.lower_value in bad for t in _significant(decl.value)):
		return []
	if bad and value.split()[0].lower() in bad:
		return []

	# A rename list may include the original name itself, e.g. opacity -> [-moz-opacity, opacity]
	return [(n, value, imp) for n in s.property_renames.get(name, [name])]


def _emit_declarations(decls):
	out = []
	for n, v, imp in decls:
		out.append(f"{n}:{v}{' !important' if imp else ''}")
	return ";".join(out)


# ----------------------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------------------

def _split_selectors(prelude):
	return [tinycss2.serialize(g).strip() for g in _split_commas(prelude) if tinycss2.serialize(g).strip()]


def _clean_selector(sel, ctx):
	if ctx.unsupported("double-colon"):
		sel = re.sub(r"::(before|after|first-line|first-letter)\b", r":\1", sel, flags=re.I)
	return sel if ctx.settings.selector_ok(sel) else None


def _process_block(selectors, content, ctx, depth):
	"""Process a style rule body. Returns a list of CSS rule strings (this rule + flattened nested rules)."""
	items = tinycss2.parse_blocks_contents(content, skip_comments=True, skip_whitespace=True)
	decls, nested = [], []
	for item in items:
		if isinstance(item, A.Declaration):
			decls.extend(_process_declaration(item, ctx))
		elif isinstance(item, (A.QualifiedRule, A.AtRule)):
			nested.append(item)

	out = []
	kept = [s for s in (_clean_selector(sel, ctx) for sel in selectors) if s]
	if kept and decls:
		out.append(f"{', '.join(kept)}{{{_emit_declarations(decls)}}}")

	for rule in nested:
		if isinstance(rule, A.QualifiedRule):
			child_sels = []
			for c in _split_selectors(rule.prelude):
				for p in selectors:
					child_sels.append(c.replace("&", p) if "&" in c else f"{p} {c}")
			out.extend(_process_block(child_sels, rule.content, ctx, depth))
		elif rule.lower_at_keyword == "media":
			if media_matches(tinycss2.serialize(rule.prelude), ctx.settings):
				out.extend(_process_block(selectors, rule.content, ctx, depth))
		elif rule.lower_at_keyword == "supports":
			if _supports_condition(tinycss2.serialize(rule.prelude), ctx.settings):
				out.extend(_process_block(selectors, rule.content, ctx, depth))
		elif rule.lower_at_keyword == "layer" and rule.content is not None:
			out.extend(_process_block(selectors, rule.content, ctx, depth))
	return out


def _import_target(rule, ctx):
	toks = _significant(rule.prelude)
	if not toks:
		return None, ""
	first = toks[0]
	if isinstance(first, A.URLToken):
		target = first.value
	elif isinstance(first, A.StringToken):
		target = first.value
	elif isinstance(first, A.FunctionBlock) and first.lower_name == "url":
		inner = _significant(first.arguments)
		target = inner[0].value if inner else None
	else:
		return None, ""
	media = tinycss2.serialize(toks[1:]).strip()
	media = re.sub(r"^(layer(\([^)]*\))?|supports\(.*?\))\s*", "", media)
	return target, media


def _join(base, target):
	from urllib.parse import urljoin
	return urljoin(base, target) if base else target


def _fetch_import(url, ctx):
	if url not in ctx.fetch_cache:
		ctx.fetch_cache[url] = fetch_text(url)
	return ctx.fetch_cache[url]


def _process_rules(rules, ctx, depth):
	out = []
	for rule in rules:
		if isinstance(rule, A.QualifiedRule):
			selectors = _split_selectors(rule.prelude)
			out.extend(_process_block(selectors, rule.content, ctx, depth))
		elif isinstance(rule, A.AtRule):
			kw = rule.lower_at_keyword
			if kw == "import":
				target, media = _import_target(rule, ctx)
				if target and depth < ctx.settings.max_import_depth and media_matches(media, ctx.settings):
					url = _join(ctx.base_url, target)
					text = _fetch_import(url, ctx)
					if text:
						sub = _Ctx(ctx.settings, ctx.vars, url, ctx.fetch_cache)
						out.extend(_process_rules(tinycss2.parse_stylesheet(text, skip_comments=True, skip_whitespace=True), sub, depth + 1))
			elif kw == "media":
				if rule.content is not None and media_matches(tinycss2.serialize(rule.prelude), ctx.settings):
					out.extend(_process_rules(tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True), ctx, depth))
			elif kw == "supports":
				if rule.content is not None and _supports_condition(tinycss2.serialize(rule.prelude), ctx.settings):
					out.extend(_process_rules(tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True), ctx, depth))
			elif kw == "layer":
				if rule.content is not None:
					out.extend(_process_rules(tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True), ctx, depth))
			# Everything else (@font-face, @keyframes, @container, @page, @namespace, @charset, ...) is dropped.
	return out


def _collect_vars(rules, ctx, depth):
	"""Gather custom properties defined on :root/html/body/* (media-evaluated, imports followed)."""
	for rule in rules:
		if isinstance(rule, A.QualifiedRule):
			if any(s in ROOT_SELECTORS for s in _split_selectors(rule.prelude)):
				for item in tinycss2.parse_blocks_contents(rule.content, skip_comments=True, skip_whitespace=True):
					if isinstance(item, A.Declaration) and item.name.startswith("--"):
						ctx.vars[item.name] = tinycss2.serialize(item.value).strip()
		elif isinstance(rule, A.AtRule) and rule.content is not None:
			kw = rule.lower_at_keyword
			if kw == "media" and not media_matches(tinycss2.serialize(rule.prelude), ctx.settings):
				continue
			if kw == "supports" and not _supports_condition(tinycss2.serialize(rule.prelude), ctx.settings):
				continue
			if kw in ("media", "supports", "layer"):
				_collect_vars(tinycss2.parse_rule_list(rule.content, skip_comments=True, skip_whitespace=True), ctx, depth)
		elif isinstance(rule, A.AtRule) and rule.lower_at_keyword == "import":
			target, media = _import_target(rule, ctx)
			if target and depth < ctx.settings.max_import_depth and media_matches(media, ctx.settings):
				url = _join(ctx.base_url, target)
				text = _fetch_import(url, ctx)
				if text:
					sub = _Ctx(ctx.settings, ctx.vars, url, ctx.fetch_cache)
					_collect_vars(tinycss2.parse_stylesheet(text, skip_comments=True, skip_whitespace=True), sub, depth + 1)


# ----------------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------------

# CSS custom properties seen per site, so a variable defined in one stylesheet can be used by
# another stylesheet or an inline style from the same site (bounded; oldest site evicted first)
_SITE_VARS = {}
_SITE_VARS_MAX = 200


def get_site_vars(url):
	from urllib.parse import urlparse
	host = urlparse(url).netloc if url else ""
	if host not in _SITE_VARS:
		if len(_SITE_VARS) >= _SITE_VARS_MAX:
			_SITE_VARS.pop(next(iter(_SITE_VARS)))
		_SITE_VARS[host] = {}
	return _SITE_VARS[host]


def collect_vars(css, settings, base_url=None, vars=None):
	"""Collect :root custom properties from a stylesheet into (and return) a vars dict."""
	vars = {} if vars is None else vars
	rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
	_collect_vars(rules, _Ctx(settings, vars, base_url), 0)
	return vars


def downlevel_css(css, settings, base_url=None, vars=None):
	"""Translate a stylesheet. `vars` (dict) may carry custom properties known from elsewhere on the page."""
	css = decode_css(css)
	vars = {} if vars is None else vars
	ctx = _Ctx(settings, vars, base_url)
	rules = tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
	if "var" in settings.unsupported_features:
		_collect_vars(rules, ctx, 0)
	out = _process_rules(rules, ctx, 0)
	return "\n".join(out).replace("https://", "http://")


def downlevel_declarations(text, settings, vars=None):
	"""Translate the contents of a style="" attribute."""
	ctx = _Ctx(settings, {} if vars is None else vars)
	items = tinycss2.parse_blocks_contents(text, skip_comments=True, skip_whitespace=True)
	decls = []
	for item in items:
		if isinstance(item, A.Declaration):
			decls.extend(_process_declaration(item, ctx))
	return _emit_declarations(decls).replace("https://", "http://")
