# Third-party imports
from bs4 import Tag

# Standard library imports
import re

"""Remove every trace of JavaScript from a parsed page (see utils/site_overrides.py)."""

JS_URL = re.compile(r"^\s*(javascript|vbscript):", re.I)
URL_ATTRS = ("href", "src", "action", "formaction", "data", "xlink:href", "poster")
# Every HTML event-handler content attribute (global, window, document and media/form/mouse/keyboard/touch/pointer ones)
EVENT_HANDLERS = frozenset("""
	abort afterprint animationcancel animationend animationiteration animationstart auxclick beforeinput
	beforematch beforeprint beforetoggle beforeunload beforexrselect blur cancel canplay canplaythrough change
	click close command contentvisibilityautostatechange contextlost contextmenu contextrestored copy cuechange
	cut dblclick drag dragend dragenter dragleave dragover dragstart drop durationchange emptied ended error
	focus focusin focusout formdata fullscreenchange fullscreenerror gesturechange gestureend gesturestart
	gotpointercapture hashchange input invalid keydown keypress keyup languagechange load loadeddata
	loadedmetadata loadstart lostpointercapture message messageerror mousedown mouseenter mouseleave mousemove
	mouseout mouseover mouseup mousewheel offline online orientationchange pagehide pagereveal pageshow
	pageswap paste pause play playing pointercancel pointerdown pointerenter pointerleave pointermove
	pointerout pointerover pointerrawupdate pointerup popstate progress ratechange readystatechange rejectionhandled
	reset resize scroll scrollend securitypolicyviolation seeked seeking select selectionchange selectstart
	slotchange stalled storage submit suspend timeupdate toggle touchcancel touchend touchmove touchstart
	transitioncancel transitionend transitionrun transitionstart unhandledrejection unload volumechange waiting
	webkitanimationend webkitanimationiteration webkitanimationstart webkittransitionend wheel
""".split())
JS_CONTENT_TYPES = (
	"text/javascript", "application/javascript", "application/x-javascript", "text/ecmascript",
	"application/ecmascript", "text/jscript", "text/livescript",
)


def is_javascript_content_type(content_type):
	return (content_type or "").split(";")[0].strip().lower() in JS_CONTENT_TYPES


def strip_javascript(soup):
	"""Drop <script> elements, script preloads, inline event handlers and javascript: URLs. Returns the number of removals."""
	removed = 0
	for tag in soup.find_all("script"):
		tag.decompose()
		removed += 1
	for tag in soup.find_all("link"):
		rel = [r.lower() for r in (tag.get("rel") or [])]
		as_ = (tag.get("as") or "").lower()
		if "modulepreload" in rel or (("preload" in rel or "prefetch" in rel) and as_ == "script"):
			tag.decompose()
			removed += 1
	for tag in soup.find_all(True):
		if not isinstance(tag, Tag) or tag.attrs is None:
			continue
		for attr in list(tag.attrs):
			low = attr.lower()
			value = tag.attrs.get(attr)
			if low.startswith("on") and low[2:] in EVENT_HANDLERS:
				del tag.attrs[attr]
				removed += 1
			elif low in URL_ATTRS and isinstance(value, str) and JS_URL.match(value):
				# Keep links clickable but inert
				tag.attrs[attr] = "#" if low in ("href", "xlink:href") else ""
				removed += 1
	# <object>/<embed>/<iframe> pointing at script-capable documents are left alone; only script itself is removed.
	return removed
