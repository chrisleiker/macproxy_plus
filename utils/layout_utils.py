# Standard library imports
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urljoin

# Third-party imports
import soupsieve
import tinycss2
from bs4 import Tag
from tinycss2 import ast as A

# First-party imports
from utils import css_utils

"""
Emulate flexbox and grid layouts for browsers that support neither, using CSS 2.1 table display
values (which even very old engines implement) plus floats.

The page's stylesheets are read on the server (inline <style> plus fetched <link> sheets), a small
cascade is computed for the properties that matter, and for every flex/grid container the layout
is re-expressed as inline styles on the container and its children:

  flex row            -> display:table container, display:table-cell children (border-spacing for gap)
  flex row, wrapping  -> floated children plus a clearing element
  flex column         -> normal block flow (gap -> margins, centered items -> shrink-wrapped tables)
  grid (N columns)    -> display:table container, rows wrapped in display:table-row, cells sized to the tracks

Anything we cannot express (grid spans / named areas, order, reversed directions, ...) falls back to
the plain stacked layout the browser would show anyway. Everything is best-effort and fail-open.
"""

FLEX_DISPLAYS = {"flex", "inline-flex", "-webkit-flex", "-webkit-inline-flex", "-ms-flexbox", "-webkit-box", "-moz-box"}
GRID_DISPLAYS = {"grid", "inline-grid", "-ms-grid"}
INLINE_DISPLAYS = {"inline-flex", "-webkit-inline-flex", "inline-grid"}
SKIP_TAGS = {"script", "style", "link", "meta", "template", "noscript", "title", "head", "br", "wbr"}
MAX_COLUMNS = 12
SHEET_TTL = 600
SHEET_CACHE_MAX = 128
FETCH_BUDGET = 8.0
CASCADE_BUDGET = 6.0

TRACKED = {
	"display", "position", "float", "width", "max-width", "flex-direction", "flex-wrap", "justify-content",
	"align-items", "align-self", "row-gap", "column-gap", "grid-template-columns", "grid-template-areas",
	"flex-grow", "flex-basis", "order", "grid-column", "grid-row", "grid-area", "grid-column-start",
	"grid-column-end", "grid-row-start", "grid-row-end", "justify-items", "place-items",
	"padding-left", "padding-right", "border-left-width", "border-right-width", "box-sizing",
	"margin-top", "margin-right", "margin-bottom", "margin-left",
}

_SHEET_CACHE = {}


class LayoutBudgetExceeded(Exception):
	pass


# ----------------------------------------------------------------------------------
# Gathering stylesheets
# ----------------------------------------------------------------------------------

def _fetch_one(url):
	now = time.time()
	hit = _SHEET_CACHE.get(url)
	if hit and now - hit[0] < SHEET_TTL:
		return hit[1]
	text = css_utils.fetch_text(url)
	if text is not None:
		if len(_SHEET_CACHE) >= SHEET_CACHE_MAX:
			_SHEET_CACHE.pop(next(iter(_SHEET_CACHE)))
		_SHEET_CACHE[url] = (now, text)
	return text


def fetch_sheets(urls):
	"""Fetch stylesheets in parallel (cached). Returns {url: text} for those that arrived in time."""
	urls = list(dict.fromkeys(urls))
	if not urls:
		return {}
	out = {}
	pool = ThreadPoolExecutor(max_workers=min(6, len(urls)))
	try:
		futures = {pool.submit(_fetch_one, u): u for u in urls}
		done, _ = wait(futures, timeout=FETCH_BUDGET)
		for f in done:
			try:
				text = f.result()
			except Exception:
				text = None
			if text:
				out[futures[f]] = text
	finally:
		pool.shutdown(wait=False)
	return out


def gather_css(soup, page_url, settings):
	"""Return [(css_text, base_url)] for the page's stylesheets, in document order."""
	entries = []  # (kind, payload)
	for tag in soup.find_all(["style", "link"]):
		if tag.name == "style":
			if tag.string and css_utils.media_matches(tag.get("media"), settings):
				entries.append(("inline", tag.string))
		else:
			rel = [r.lower() for r in (tag.get("rel") or [])]
			href = tag.get("href")
			if "stylesheet" in rel and "alternate" not in rel and href and css_utils.media_matches(tag.get("media"), settings):
				entries.append(("link", urljoin(page_url, href) if page_url else href))
	fetched = fetch_sheets([p for k, p in entries if k == "link"])
	sheets = []
	for kind, payload in entries:
		if kind == "inline":
			sheets.append((payload, page_url))
		elif payload in fetched:
			sheets.append((fetched[payload], payload))
	return sheets


# ----------------------------------------------------------------------------------
# Rules and cascade
# ----------------------------------------------------------------------------------

def _resolver_settings(settings):
	"""Settings that resolve var()/calc()/units to plain values but keep every property and selector."""
	return css_utils.CSSSettings(
		viewport_width=settings.viewport_width,
		viewport_height=settings.viewport_height,
		unsupported_features=frozenset({"var", "calc", "minmax", "rem", "viewport-units", "new-viewport-units"}),
		drop_vendor_prefixes=(),
		strip_at_rules=frozenset({"font-face", "keyframes", "-webkit-keyframes", "counter-style", "property", "namespace", "charset"}),
	)


def specificity(selector):
	s = re.sub(r"\"[^\"]*\"|'[^']*'", "", selector)
	attrs = len(re.findall(r"\[[^\]]*\]", s))
	s = re.sub(r"\[[^\]]*\]", "", s)
	ids = len(re.findall(r"#[\w-]+", s))
	classes = len(re.findall(r"\.[\w-]+", s)) + attrs + len(re.findall(r"(?<!:):(?!:)[\w-]+", s))
	types = len(re.findall(r"(?:^|[\s>+~(,])([a-zA-Z][\w-]*)", s)) + len(re.findall(r"::[\w-]+", s))
	return (ids, classes, types)


def _split_value(value):
	return [t for t in tinycss2.parse_component_value_list(value) if not isinstance(t, (A.WhitespaceToken, A.Comment))]


def _expand(name, value):
	"""Expand shorthands into the longhands we track. Returns [(name, value)]."""
	name = name.lower()
	value = value.strip()
	low = value.lower()
	if name == "flex":
		if low == "none":
			return [("flex-grow", "0"), ("flex-basis", "auto")]
		if low == "auto":
			return [("flex-grow", "1"), ("flex-basis", "auto")]
		toks = _split_value(value)
		nums = [t for t in toks if isinstance(t, A.NumberToken)]
		lens = [t for t in toks if isinstance(t, (A.DimensionToken, A.PercentageToken))]
		grow = nums[0].value if nums else 1
		if lens:
			basis = lens[0].serialize()
		elif len(nums) >= 3:
			basis = "0"
		else:
			basis = "0"
		return [("flex-grow", css_utils._fmt(grow)), ("flex-basis", basis)]
	if name == "flex-flow":
		out = []
		for t in _split_value(value):
			if isinstance(t, A.IdentToken):
				v = t.lower_value
				if v.startswith(("row", "column")):
					out.append(("flex-direction", v))
				elif v in ("wrap", "nowrap", "wrap-reverse"):
					out.append(("flex-wrap", v))
		return out
	if name in ("gap", "grid-gap"):
		parts = [t.serialize() for t in _split_value(value)]
		if not parts:
			return []
		return [("row-gap", parts[0]), ("column-gap", parts[1] if len(parts) > 1 else parts[0])]
	if name == "grid-column-gap":
		return [("column-gap", value)]
	if name == "grid-row-gap":
		return [("row-gap", value)]
	if name == "place-items":
		parts = low.split()
		return [("align-items", parts[0]), ("justify-items", parts[-1])] if parts else []
	if name == "margin":
		vals = [t.serialize() for t in _split_value(value) if not isinstance(t, A.LiteralToken)]
		if not vals or len(vals) > 4:
			return []
		top, right = vals[0], vals[1] if len(vals) > 1 else vals[0]
		bottom = vals[2] if len(vals) > 2 else top
		left = vals[3] if len(vals) > 3 else right
		return [("margin-top", top), ("margin-right", right), ("margin-bottom", bottom), ("margin-left", left)]
	if name in ("padding", "border-width"):
		parts = [t for t in _split_value(value) if not isinstance(t, A.LiteralToken)]
		vals = [t.serialize() for t in parts]
		if not vals or len(vals) > 4:
			return []
		top, right = vals[0], vals[1] if len(vals) > 1 else vals[0]
		bottom = vals[2] if len(vals) > 2 else top
		left = vals[3] if len(vals) > 3 else right
		if name == "padding":
			return [("padding-left", left), ("padding-right", right)]
		return [("border-left-width", left), ("border-right-width", right)]
	if name in ("border", "border-left", "border-right"):
		width = "0" if "none" in low.split() or low in ("0", "hidden") else None
		if width is None:
			for t in _split_value(value):
				if isinstance(t, A.DimensionToken) or (isinstance(t, A.NumberToken) and t.value == 0):
					width = t.serialize()
					break
				if isinstance(t, A.IdentToken) and t.lower_value in ("thin", "medium", "thick"):
					width = {"thin": "1px", "medium": "3px", "thick": "5px"}[t.lower_value]
					break
		if width is None:
			width = "3px"
		out = []
		if name in ("border", "border-left"):
			out.append(("border-left-width", width))
		if name in ("border", "border-right"):
			out.append(("border-right-width", width))
		return out
	if name == "-webkit-box-flex":
		return [("flex-grow", value)]
	if name in TRACKED:
		return [(name, low if name not in ("grid-template-columns", "grid-template-areas") else value)]
	return []


class Rule:
	__slots__ = ("selector", "spec", "order", "decls")

	def __init__(self, selector, spec, order, decls):
		self.selector, self.spec, self.order, self.decls = selector, spec, order, decls


def parse_declarations(text):
	"""Parse a declaration list into {prop: (value, important)}, expanding tracked shorthands."""
	out = {}
	for item in tinycss2.parse_blocks_contents(text, skip_comments=True, skip_whitespace=True):
		if isinstance(item, A.Declaration):
			value = tinycss2.serialize(item.value).strip()
			for n, v in _expand(item.lower_name, value):
				out[n] = (v, item.important)
	return out


_RULE_CACHE = {}  # (sheet url, viewport) -> (timestamp, [Rule]) parsed layout rules, so a stylesheet is parsed once


def _rules_from_sheet(text, base, resolver, site_vars):
	resolved = css_utils.downlevel_css(text, resolver, base, site_vars)
	rules, order = [], 0
	for rule in tinycss2.parse_stylesheet(resolved, skip_comments=True, skip_whitespace=True):
		if not isinstance(rule, A.QualifiedRule):
			continue
		decls = parse_declarations(tinycss2.serialize(rule.content))
		if not decls:
			continue
		for sel in css_utils._split_selectors(rule.prelude):
			if "::" in sel:
				continue
			order += 1
			rules.append(Rule(sel, specificity(sel), order, decls))
	return rules


def build_rules(sheets, settings, site_vars):
	"""Resolve each stylesheet (imports, media, nesting, var(), calc()) and extract the rules that set layout properties."""
	resolver = _resolver_settings(settings)
	rules, offset = [], 0
	now = time.time()
	for text, base in sheets:
		# Keyed by content too: inline <style> blocks all share the page URL as their base
		key = (base, hash(text), settings.viewport_width, settings.viewport_height) if len(text) > 2000 else None
		hit = _RULE_CACHE.get(key) if key else None
		if hit and now - hit[0] < SHEET_TTL:
			sheet_rules = hit[1]
		else:
			try:
				sheet_rules = _rules_from_sheet(text, base, resolver, site_vars)
			except Exception as e:
				print(f"Layout: could not resolve stylesheet {base}: {e}")
				continue
			if key:
				if len(_RULE_CACHE) >= SHEET_CACHE_MAX:
					_RULE_CACHE.pop(next(iter(_RULE_CACHE)))
				_RULE_CACHE[key] = (now, sheet_rules)
		for r in sheet_rules:
			rules.append(Rule(r.selector, r.spec, offset + r.order, r.decls))
		offset += len(sheet_rules)
	return rules


def _strip_selector(selector):
	"""Remove strings, attribute selectors and functional pseudo-classes (:not(), :is(), :nth-child(), ...)."""
	sel = re.sub(r"\"[^\"]*\"|'[^']*'", "", selector)
	sel = re.sub(r"\[[^\]]*\]", "", sel)
	out, depth, i = [], 0, 0
	while i < len(sel):
		ch = sel[i]
		if ch == ":":
			m = re.match(r":+[\w-]+\(", sel[i:])
			if m:
				depth_start = i + m.end()
				d, k = 1, depth_start
				while k < len(sel) and d:
					d += (sel[k] == "(") - (sel[k] == ")")
					k += 1
				i = k
				continue
		out.append(ch)
		i += 1
	return "".join(out)


_TOKEN_RE = re.compile(r"\.((?:\\.|[\w-])+)|#((?:\\.|[\w-])+)")


def _unescape(tok):
	return re.sub(r"\\(.)", r"\1", tok)


def selector_tokens(selector):
	"""(required classes, required ids, last-compound classes, last-compound ids, last-compound tag, reliable)."""
	sel = _strip_selector(selector)
	reliable = not re.search(r"\\[0-9a-fA-F]", sel)  # hex escapes are not unescaped; do not prune on them
	classes, ids = set(), set()
	for m in _TOKEN_RE.finditer(sel):
		if m.group(1):
			classes.add(_unescape(m.group(1)))
		else:
			ids.add(_unescape(m.group(2)))
	last = re.split(r"[\s>+~]+", sel.strip())[-1] if sel.strip() else ""
	lclasses, lids = set(), set()
	for m in _TOKEN_RE.finditer(last):
		if m.group(1):
			lclasses.add(_unescape(m.group(1)))
		else:
			lids.add(_unescape(m.group(2)))
	tm = re.match(r"[a-zA-Z][\w-]*", last)
	return classes, ids, lclasses, lids, (tm.group(0).lower() if tm else None), reliable


class DocIndex:
	"""Index elements by class, id and tag so a rule only has to test the few elements it could match."""

	def __init__(self, elements):
		self.all = list(elements)
		self.by_class, self.by_id, self.by_tag = {}, {}, {}
		for el in self.all:
			self.by_tag.setdefault(el.name, []).append(el)
			if el.get("id"):
				self.by_id.setdefault(el["id"], []).append(el)
			for c in el.get("class") or []:
				self.by_class.setdefault(c, []).append(el)
		self._token_cache = {}

	def candidates(self, selector):
		toks = self._token_cache.get(selector)
		if toks is None:
			toks = self._token_cache[selector] = selector_tokens(selector)
		classes, ids, lclasses, lids, tag, reliable = toks
		if reliable:
			if any(c not in self.by_class for c in classes) or any(i not in self.by_id for i in ids):
				return []  # the page has no element with a class/id this selector requires
			lists = [self.by_id[i] for i in lids] + [self.by_class[c] for c in lclasses]
			if tag and tag != "*":
				lists.append(self.by_tag.get(tag, []))
			if lists:
				return min(lists, key=len)
			return self.all
		return self.all


def cascade(index, rules, deadline):
	"""Compute {id(tag): {prop: (important, spec, order, value)}} for the elements in `index` across all rules."""
	best = {}
	tags = {}
	for rule in rules:
		if time.time() > deadline:
			raise LayoutBudgetExceeded()
		for el in index.candidates(rule.selector):
			try:
				if not soupsieve.match(rule.selector, el):
					continue
			except Exception:
				break  # unsupported selector syntax: skip the whole rule
			slot = best.setdefault(id(el), {})
			tags[id(el)] = el
			for prop, (value, important) in rule.decls.items():
				key = (important, rule.spec, rule.order)
				if prop not in slot or key >= slot[prop][:3]:
					slot[prop] = key + (value,)
	return best, tags


def element_props(el, best, inline_props):
	props = {k: v[3] for k, v in best.get(id(el), {}).items()}
	for k, (v, important) in inline_props.items():
		slot = best.get(id(el), {}).get(k)
		if slot is None or important or not slot[0]:
			props[k] = v
	return props


# ----------------------------------------------------------------------------------
# Value helpers
# ----------------------------------------------------------------------------------

def px_value(value, base=None):
	"""'12px' -> 12.0, '0' -> 0.0, '1.5em' -> 24.0. Returns None for anything else (auto, %, ...)."""
	if value is None:
		return None
	m = re.fullmatch(r"\s*(-?[\d.]+)\s*(px|em|rem|pt)?\s*", str(value))
	if not m:
		return None
	n, unit = float(m.group(1)), m.group(2)
	if unit in ("em", "rem"):
		return n * 16
	if unit == "pt":
		return n * 96 / 72
	return n


def pct_value(value):
	m = re.fullmatch(r"\s*(-?[\d.]+)%\s*", str(value or ""))
	return float(m.group(1)) if m else None


def _fmt(n):
	return css_utils._fmt(n)


def box_sizing(el, props_of):
	"""Resolve box-sizing, following 'inherit' up the tree (the usual `*{box-sizing:inherit}` reset)."""
	node = el
	while isinstance(node, Tag):
		value = props_of(node).get("box-sizing")
		if value and value != "inherit":
			return value
		if value is None and node is el:
			pass
		node = node.parent
	return "content-box"


def horizontal_extras(props):
	"""Horizontal padding + border widths (px) of an element, 0 for anything we cannot read."""
	return sum(px_value(props.get(k)) or 0 for k in ("padding-left", "padding-right", "border-left-width", "border-right-width"))


def available_width(el, props_of, settings):
	"""Estimate the content width (px) of `el`'s parent: the viewport narrowed by every ancestor's width,
	max-width and horizontal padding/border. Percentages in the emulated layout resolve against this."""
	chain = []
	node = el.parent
	while isinstance(node, Tag) and node.name not in ("[document]",):
		chain.append(node)
		node = node.parent
	avail = float(settings.viewport_width)
	for node in reversed(chain):
		p = props_of(node)
		extras = horizontal_extras(p)
		own = px_value(p.get("width")) or px_value(p.get("max-width"))
		if own:
			content = own - extras if box_sizing(node, props_of) == "border-box" else own
			avail = min(avail - extras, content)
		else:
			avail -= extras
	return max(avail, 100.0)


def margin_px(props, key):
	"""A margin in px: 0 when unset, None when it is auto/a percentage/anything we cannot compensate for."""
	value = props.get(key)
	return 0.0 if value is None else px_value(value)


def spacing_compensation(props, cgap, rgap, horizontal=True, vertical=True, left=True):
	"""border-spacing (and trailing float margins) also inset a table's outer edges by the gap, which flex gap
	does not. Cancel that with negative margins, relative to the site's own px margins. Returns
	(margin pairs, extra width in px)."""
	pairs, widen = [], 0.0
	ml, mr, mt, mb = (margin_px(props, f"margin-{k}") for k in ("left", "right", "top", "bottom"))
	if horizontal and cgap and mr is not None and (ml is not None or not left):
		if left:
			pairs.append(("margin-left", f"{_fmt(ml - cgap)}px"))
			widen += cgap
		pairs.append(("margin-right", f"{_fmt(mr - cgap)}px"))
		widen += cgap
	if vertical and rgap and mt is not None and mb is not None:
		if left:
			pairs.append(("margin-top", f"{_fmt(mt - rgap)}px"))
		pairs.append(("margin-bottom", f"{_fmt(mb - rgap)}px"))
	return pairs, widen


TABLE_BOX = [("box-sizing", "content-box"), ("-moz-box-sizing", "content-box")]


def container_width_css(props, cw, extras, sizing, widen=0.0, viewport=None):
	"""Width to give a table that replaces a block-level flex/grid container.

	A real flex container fills its parent's width including its own padding and borders. A table sized
	100% would overflow by that padding, so subtract an estimate of it."""
	maxw = px_value(props.get("max-width"))
	if maxw:
		# max-width is a content width unless the site uses border-box; tables need a plain content width
		width = min(maxw - (extras if sizing == "border-box" else 0), cw)
		return f"{_fmt(max(width + widen, 0))}px"
	if extras > 0 or widen > 0:
		return f"{_fmt(max(5.0, (cw - extras + widen) / cw * 100))}%"
	return "100%"


def kind_of(display):
	d = (display or "").strip().lower().split(" ")[0]
	if d in FLEX_DISPLAYS:
		return "flex", d in INLINE_DISPLAYS
	if d in GRID_DISPLAYS:
		return "grid", d in INLINE_DISPLAYS
	return None, False


def parse_tracks(value, container_width, gap):
	"""Parse grid-template-columns into a list of ('fr', n) | ('px', n) | ('pct', n) | ('auto',)."""
	def one(tok):
		if isinstance(tok, A.DimensionToken):
			u = tok.lower_unit
			if u == "fr":
				return ("fr", tok.value)
			if u == "px":
				return ("px", tok.value)
			if u in ("em", "rem"):
				return ("px", tok.value * 16)
			return ("auto",)
		if isinstance(tok, A.NumberToken) and tok.value == 0:
			return ("px", 0.0)
		if isinstance(tok, A.PercentageToken):
			return ("pct", tok.value)
		if isinstance(tok, A.FunctionBlock) and tok.lower_name == "minmax":
			args = [[t for t in g if not isinstance(t, A.WhitespaceToken)] for g in css_utils._split_commas(tok.arguments)]
			if len(args) == 2 and args[1]:
				hi = one(args[1][0])
				if hi[0] != "auto":
					return hi
				lo = one(args[0][0]) if args[0] else ("auto",)
				return lo
			return ("auto",)
		return ("auto",)

	def min_of(tok):
		"""Minimum px size of a track, used to count auto-fill columns."""
		if isinstance(tok, A.FunctionBlock) and tok.lower_name == "minmax":
			args = [[t for t in g if not isinstance(t, A.WhitespaceToken)] for g in css_utils._split_commas(tok.arguments)]
			if args and args[0]:
				t = one(args[0][0])
				return t[1] if t[0] == "px" else None
		t = one(tok)
		return t[1] if t[0] == "px" else None

	tracks = []
	for tok in _split_value(value):
		if isinstance(tok, A.SquareBracketsBlock):
			continue
		if isinstance(tok, A.FunctionBlock) and tok.lower_name == "repeat":
			groups = css_utils._split_commas(tok.arguments)
			if len(groups) < 2:
				return None
			head = [t for t in groups[0] if not isinstance(t, A.WhitespaceToken)]
			inner_toks = []
			for i, g in enumerate(groups[1:]):
				if i:
					inner_toks.append(A.LiteralToken(0, 0, ","))
				inner_toks.extend(g)
			inner_value = tinycss2.serialize(inner_toks)
			inner = parse_tracks(inner_value, container_width, gap) or []
			if not head or not inner:
				return None
			if isinstance(head[0], A.NumberToken):
				count = int(head[0].value)
			elif isinstance(head[0], A.IdentToken) and head[0].lower_value in ("auto-fill", "auto-fit"):
				inner_toks_sig = [t for t in inner_toks if not isinstance(t, (A.WhitespaceToken, A.LiteralToken))]
				mins = [min_of(t) for t in inner_toks_sig[:1]]
				minw = mins[0] if mins and mins[0] else 200.0
				count = max(1, int((container_width + gap) // (minw + gap)))
			else:
				return None
			tracks.extend(inner * min(count, MAX_COLUMNS))
		else:
			tracks.append(one(tok))
	return tracks[:MAX_COLUMNS] or None


# ----------------------------------------------------------------------------------
# Planning: turn containers into inline-style edits
# ----------------------------------------------------------------------------------

class Plan:
	def __init__(self):
		self.container_styles = {}  # id(tag) -> (tag, pairs): the element's own layout as a flex/grid container
		self.item_styles = {}       # id(tag) -> (tag, pairs): the element's layout as an item of its parent
		self.rows = []              # (container, [[child,...], ...]) grid row wrappers
		self.clears = []            # containers needing a clearing element appended

	def container(self, tag, pairs):
		self.container_styles[id(tag)] = (tag, pairs)

	def item(self, tag, pairs, grows=False):
		self.item_styles[id(tag)] = (tag, pairs, grows)


VALIGN = {"center": "middle", "flex-end": "bottom", "end": "bottom", "self-end": "bottom", "baseline": "baseline",
		  "flex-start": "top", "start": "top", "self-start": "top", "stretch": "top", "normal": "top"}


def _in_flow_children(el, props_of):
	kids = []
	for c in el.children:
		if not isinstance(c, Tag) or c.name in SKIP_TAGS or c.has_attr("hidden"):
			continue
		p = props_of(c)
		d = (p.get("display") or "").split(" ")[0]
		if d == "none" or p.get("position") in ("absolute", "fixed"):
			continue
		kids.append((c, p))
	return kids


def _container_width(props, settings):
	w = px_value(props.get("width")) or px_value(props.get("max-width"))
	return min(w, settings.viewport_width) if w else float(settings.viewport_width)


def plan_flex(el, props, is_inline, kids, plan, settings, extras=0.0, sizing="content-box", cw=None):
	direction = props.get("flex-direction", "row")
	wrap = props.get("flex-wrap", "nowrap") not in ("nowrap", "")
	justify = props.get("justify-content", "flex-start")
	align = props.get("align-items", "stretch")
	cgap = px_value(props.get("column-gap")) or 0
	rgap = px_value(props.get("row-gap")) or 0
	n = len(kids)
	cw = cw or float(settings.viewport_width)

	if direction in ("row-reverse", "column-reverse") or any(p.get("order") not in (None, "0") for _, p in kids):
		# Cannot reorder safely; stack the items (the browser's default) rather than showing them wrongly
		plan.container(el, [("display", "block")])
		return

	if direction.startswith("column"):
		plan.container(el, [("display", "inline-block" if is_inline else "block")])
		for i, (child, p) in enumerate(kids):
			pairs = []
			if rgap and i > 0:
				pairs.append(("margin-top", f"{_fmt(rgap)}px"))
			if align in ("center",) and p.get("align-self") in (None, "auto", "center"):
				pairs += [("display", "table"), ("margin-left", "auto"), ("margin-right", "auto")]
			elif align in ("flex-end", "end") or p.get("align-self") in ("flex-end", "end"):
				pairs += [("display", "table"), ("margin-left", "auto")]
			if pairs:
				plan.item(child, pairs)
		return

	if wrap:
		# The row gap goes on the TOP of each float (not the bottom): a margin below the last row would sit inside the
		# float's margin box, and a following table or other BFC cannot overlap that. The container is pulled up to match.
		comp = []
		mt, mr = margin_px(props, "margin-top"), margin_px(props, "margin-right")
		if rgap and mt is not None:
			comp.append(("margin-top", f"{_fmt(mt - rgap)}px"))
		if cgap and mr is not None:
			comp.append(("margin-right", f"{_fmt(mr - cgap)}px"))
		plan.container(el, [("display", "block")] + comp)
		plan.clears.append(el)
		for child, p in kids:
			pairs = [("float", "left"), ("display", "block")]
			basis = p.get("flex-basis")
			if basis not in (None, "auto", "0") and not p.get("width"):
				pairs.append(("width", basis))
			if cgap:
				pairs.append(("margin-right", f"{_fmt(cgap)}px"))
			if rgap:
				pairs.append(("margin-top", f"{_fmt(rgap)}px"))
			plan.item(child, pairs)
		return

	# Single-line row -> table
	cont = [("display", "inline-table" if is_inline else "table")] + TABLE_BOX
	has_width = bool(props.get("width"))
	comp, widen = spacing_compensation(props, cgap, 0, vertical=False)
	if justify in ("center", "flex-end", "end", "right"):
		cont.append(("margin-left", "auto"))
		if justify == "center":
			cont.append(("margin-right", "auto"))
	else:
		cont += comp
		if not has_width and not is_inline:
			# Tables ignore max-width in old engines, so pin the width (the site's own auto margins still centre it)
			cont.append(("width", container_width_css(props, cw, extras, sizing, widen)))
	if cgap:
		cont.append(("border-spacing", f"{_fmt(cgap)}px 0"))
	plan.container(el, cont)

	grows = [float(p.get("flex-grow") or 0) for _, p in kids]
	all_grow = n > 0 and all(g > 0 for g in grows)
	total = sum(grows) or 1
	for i, (child, p) in enumerate(kids):
		pairs = [("display", "table-cell"), ("float", "none")]
		v = VALIGN.get(p.get("align-self") if p.get("align-self") not in (None, "auto") else align, "top")
		pairs.append(("vertical-align", v))
		basis = p.get("flex-basis")
		if all_grow and not p.get("width") and basis in (None, "auto", "0"):
			pairs.append(("width", f"{_fmt(grows[i] / total * 100)}%"))
		elif basis not in (None, "auto", "0") and not p.get("width"):
			pairs.append(("width", basis))
		if justify in ("space-between", "space-around", "space-evenly") and n > 1:
			pairs.append(("text-align", "left" if i == 0 else "right" if i == n - 1 else "center"))
		plan.item(child, pairs, grows=grows[i] > 0)


def plan_grid(el, props, is_inline, kids, plan, settings, extras=0.0, sizing="content-box", cw=None):
	rgap = px_value(props.get("row-gap")) or 0
	cgap = px_value(props.get("column-gap")) or 0
	cw = cw or _container_width(props, settings)
	own = px_value(props.get("width")) or px_value(props.get("max-width"))
	inner_width = max((min(cw, own) if own else cw) - extras, 50.0)  # width the grid tracks are laid out in
	tracks = parse_tracks(props["grid-template-columns"], inner_width, cgap) if props.get("grid-template-columns") else None
	placed = any(
		(p.get(k) not in (None, "auto")) for _, p in kids
		for k in ("grid-column", "grid-row", "grid-area", "grid-column-start", "grid-column-end", "grid-row-start", "grid-row-end"))
	if not tracks or len(tracks) < 2 or placed or props.get("grid-template-areas") not in (None, "none"):
		# One column (or a layout we can't express): ordinary stacking with the row gap between items
		plan.container(el, [("display", "inline-block" if is_inline else "block")])
		for i, (child, p) in enumerate(kids):
			if rgap and i > 0:
				plan.item(child, [("margin-top", f"{_fmt(rgap)}px")])
		return

	ncols = len(tracks)
	align = props.get("align-items", "stretch")
	cont = [("display", "inline-table" if is_inline else "table"), ("border-spacing", f"{_fmt(cgap)}px {_fmt(rgap)}px")] + TABLE_BOX
	comp, widen = spacing_compensation(props, cgap, rgap)
	cont += comp
	if not props.get("width") and not is_inline:
		cont.append(("width", container_width_css(props, cw, extras, sizing, widen)))
	plan.container(el, cont)

	# Column widths
	fixed_px = sum(t[1] for t in tracks if t[0] == "px")
	fixed_pct = sum(t[1] for t in tracks if t[0] == "pct")
	total_fr = sum(t[1] for t in tracks if t[0] == "fr")
	gap_pct = (cgap * (ncols - 1) + fixed_px) / inner_width * 100
	fr_share = max(5.0, 100.0 - fixed_pct - gap_pct)
	widths = []
	for t in tracks:
		if t[0] == "fr" and total_fr:
			widths.append(f"{_fmt(t[1] / total_fr * fr_share)}%")
		elif t[0] == "px":
			widths.append(f"{_fmt(t[1])}px")
		elif t[0] == "pct":
			widths.append(f"{_fmt(t[1])}%")
		else:
			widths.append(None)

	rows = [[c for c, _ in kids[i:i + ncols]] for i in range(0, len(kids), ncols)]
	plan.rows.append((el, rows))
	for r, row in enumerate(rows):
		for c, child in enumerate(row):
			p = dict(kids[r * ncols + c][1])
			a = p.get("align-self") if p.get("align-self") not in (None, "auto") else align
			pairs = [("display", "table-cell"), ("float", "none"), ("vertical-align", VALIGN.get(a, "top"))]
			if widths[c] and not p.get("width"):
				pairs.append(("width", widths[c]))
			plan.item(child, pairs, grows=True)


def _add_style(tag, pairs):
	existing = (tag.get("style") or "").strip().rstrip(";")
	add = ";".join(f"{k}:{v}" for k, v in pairs)
	tag["style"] = f"{existing};{add}" if existing else add


def emulate_layout(soup, page_url, settings, site_vars=None):
	"""Rewrite flex/grid containers in `soup` so older browsers lay them out like modern ones.
	Must run BEFORE the page's CSS is downleveled (the layout properties are read from the original CSS)."""
	started = time.time()
	deadline = started + CASCADE_BUDGET
	try:
		sheets = gather_css(soup, page_url, settings)
		rules = build_rules(sheets, settings, site_vars if site_vars is not None else {})
		resolver = _resolver_settings(settings)
		t_rules = time.time()

		# Inline styles take part in the cascade
		inline_cache = {}

		def inline_of(el):
			if id(el) not in inline_cache:
				text = el.get("style")
				if text:
					try:
						resolved = css_utils.downlevel_declarations(text, resolver, site_vars)
						inline_cache[id(el)] = parse_declarations(resolved)
					except Exception:
						inline_cache[id(el)] = {}
				else:
					inline_cache[id(el)] = {}
			return inline_cache[id(el)]

		# Phase A: find the elements that might be flex/grid containers (cheap: only rules that set such a display)
		all_tags = soup.find_all(True)
		index_all = DocIndex(all_tags)
		candidates = {}
		for rule in rules:
			if "display" in rule.decls and kind_of(rule.decls["display"][0])[0]:
				for el in index_all.candidates(rule.selector):
					try:
						if soupsieve.match(rule.selector, el):
							candidates[id(el)] = el
					except Exception:
						break
		for el in soup.find_all(style=True):
			if re.search(r"display\s*:\s*(inline-)?(flex|grid)|-webkit-box", el.get("style", ""), re.I):
				candidates[id(el)] = el
		if not candidates:
			return 0

		# Phase B: full cascade, but only for the candidates, their children and their ancestors
		relevant = {}
		for el in candidates.values():
			relevant[id(el)] = el
			for child in el.children:
				if isinstance(child, Tag):
					relevant[id(child)] = child
			for parent in el.parents:
				if isinstance(parent, Tag):
					relevant[id(parent)] = parent
		best, tags = cascade(DocIndex(relevant.values()), [r for r in rules if r.decls.keys() & TRACKED], deadline)
		for el in relevant.values():
			best.setdefault(id(el), {})

		def props_of(el):
			return element_props(el, best, inline_of(el))

		plan = Plan()
		containers = []
		for el in relevant.values():
			kind, inline = kind_of(props_of(el).get("display"))
			if kind:
				containers.append((el, kind, inline))

		for el, kind, inline in containers:
			props = props_of(el)
			kids = _in_flow_children(el, props_of)
			if not kids:
				plan.container(el, [("display", "inline-block" if inline else "block")])
				continue
			extras = horizontal_extras(props)
			sizing = box_sizing(el, props_of)
			cw = available_width(el, props_of, settings)
			if kind == "flex":
				plan_flex(el, props, inline, kids, plan, settings, extras, sizing, cw)
			else:
				plan_grid(el, props, inline, kids, plan, settings, extras, sizing, cw)
		print(f"Layout emulation: {len(containers)} containers, {len(rules)} rules "
			  f"(rules {t_rules - started:.1f}s, cascade {time.time() - t_rules:.1f}s)")
	except LayoutBudgetExceeded:
		print("Layout emulation skipped: took too long")
		return 0
	except Exception as e:
		print(f"Layout emulation failed: {e}")
		return 0

	# Apply. An element that is both a table-cell item and a container needs an inner wrapper to carry the
	# container layout, because one element cannot be a cell and a table at once.
	target = {}
	for key, (tag, pairs, grows) in plan.item_styles.items():
		_add_style(tag, pairs)
	for key, (tag, pairs) in plan.container_styles.items():
		item = plan.item_styles.get(key)
		if item and dict(item[1]).get("display") == "table-cell":
			inner = soup.new_tag("div")
			for child in list(tag.contents):
				inner.append(child.extract())
			tag.append(inner)
			target[key] = inner
			if not item[2]:
				# A content-sized flex item shrink-wraps its own flex container; it must not stretch to the
				# (wider) cell, and it keeps the alignment the parent gave the cell.
				align = dict(item[1]).get("text-align")
				pairs = [(k, v) for k, v in pairs if k != "width"]
				if align == "right":
					pairs.append(("margin-left", "auto"))
				elif align == "center":
					pairs += [("margin-left", "auto"), ("margin-right", "auto")]
			_add_style(inner, pairs)
		else:
			target[key] = tag
			_add_style(tag, pairs)
	for container, rows in plan.rows:
		for row in rows:
			wrapper = soup.new_tag("div", style="display:table-row")
			row[0].insert_before(wrapper)
			for child in row:
				wrapper.append(child.extract())
	for container in plan.clears:
		target.get(id(container), container).append(
			soup.new_tag("div", style="clear:both;height:0;overflow:hidden;font-size:0;line-height:0"))
	return len(plan.container_styles)
