# Standard library imports
import re

# Third-party imports
from bs4 import NavigableString, Tag
from bs4.element import CData, Comment, Declaration, Doctype, ProcessingInstruction

"""
Remove advertisement slots that are empty.

Sites reserve room for an ad with CSS (a wrapper with a `min-height: 250px` and generous margins), and the ad itself is
filled in later by a script. Here the scripts are gone (or the ad was blocked), so the reservation stays: a blank band
across the top of the page. A slot is only removed when it is named like an ad slot, by whole words, and nothing is
inside it: no text, no picture, no frame, no control.
"""

# Whole words (the parts of a class or id name between - _ and camelCase breaks) that mark an ad container
AD_WORDS = {"ad", "ads", "advert", "adverts", "advertisement", "advertisements", "advertising", "adslot", "adslots", "adunit",
			"adunits", "adwrapper", "adbox", "adcontainer", "adspace", "adsbygoogle", "adbanner", "adplaceholder", "dfp", "gpt",
			"sponsored", "sponsor"}
# ... but not when they are part of one of these (they are about something else)
NOT_ADS = {"sponsorship"}
CONTENT_TAGS = {"img", "picture", "video", "audio", "canvas", "iframe", "object", "embed", "svg", "input", "button", "select",
				"textarea", "form", "table", "hr", "map", "frame", "frameset", "math"}
NOT_CONTENT_TAGS = {"script", "style", "noscript", "template", "link", "meta"}
CANDIDATE_TAGS = {"div", "aside", "section", "p", "span", "li", "figure", "ins", "center", "article"}
_NOT_TEXT = (Comment, Doctype, Declaration, ProcessingInstruction, CData)
MAX_PARENT_LEVELS = 2


def words_of(name):
	"""'ad-wrapper' -> {'ad', 'wrapper'}; 'adSlot_top' -> {'ad', 'slot', 'top'}; 'download' -> {'download'}."""
	spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
	return {w.lower() for w in re.split(r"[\s_\-.:]+", spaced) if w}


def looks_like_an_ad_slot(el):
	if el.name not in CANDIDATE_TAGS:
		return False
	names = list(el.get("class") or []) + ([el["id"]] if el.get("id") else [])
	for name in names:
		words = words_of(name)
		if words & NOT_ADS:
			continue
		if words & AD_WORDS or name.lower() in AD_WORDS:
			return True
		# glued words such as "adslot1" or "advert2": a known word followed only by digits
		if re.fullmatch(r"(?:ad|ads|advert|adslot|adunit|dfp|gpt)\d+", name.lower()):
			return True
	return False


def is_empty(el):
	"""Nothing a person could see inside: no text, no picture, frame or control."""
	for node in el.descendants:
		if isinstance(node, NavigableString):
			if not isinstance(node, _NOT_TEXT) and str(node).strip() and not _inside_ignored(node, el):
				return False
		elif isinstance(node, Tag) and node.name in CONTENT_TAGS:
			return False
	return True


def _inside_ignored(text, root):
	for parent in text.parents:
		if parent is root:
			return False
		if isinstance(parent, Tag) and parent.name in NOT_CONTENT_TAGS:
			return True
	return False


def _only_inert_left(parent):
	"""Is a wrapper empty now that the slot is gone: no other boxes, no text, nothing but scripts and the like?"""
	return all(c.name in NOT_CONTENT_TAGS for c in parent.find_all(True, recursive=False)) and is_empty(parent)


def collapse(soup):
	"""Remove empty ad slots (and the wrappers that held only them). Returns how many elements were removed."""
	removed = 0
	for el in soup.find_all(list(CANDIDATE_TAGS)):
		if el.decomposed or el.parent is None or not looks_like_an_ad_slot(el) or not is_empty(el):
			continue
		parent = el.parent
		el.decompose()
		removed += 1
		# A wrapper that only held the slot is just as empty now
		for _ in range(MAX_PARENT_LEVELS):
			if not isinstance(parent, Tag) or parent.name in ("body", "html", "main", "header", "footer", "nav", "[document]"):
				break
			if not _only_inert_left(parent):
				break
			grand = parent.parent
			parent.decompose()
			removed += 1
			parent = grand
	return removed
