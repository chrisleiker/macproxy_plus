# Standard library imports
import ipaddress
import os
import re
import tempfile
import threading
import time
from collections import Counter
from urllib.parse import urljoin, urlparse

# Third-party imports
import requests

"""
Ad and tracker blocking, in three layers:

  1. Requests the browser makes through the proxy to ad/tracker URLs are answered with an empty stand-in.
  2. The headless Chromium (RENDER_JAVASCRIPT) is not allowed to load them either, which also makes rendering faster.
  3. Elements left in a page are removed: tags that load a blocked URL, and anything the filter lists' "element hiding"
     rules match (utils/adblock.py: apply_to_soup).

Filter lists use the Adblock Plus / uBlock syntax (EasyList, EasyPrivacy) or the hosts-file format. Supported:
  network rules   ||domain^  |http://  /path/  wildcards (*) and separators (^)  regex rules  @@ exceptions
                  options: script image stylesheet subdocument xmlhttprequest object media font ping other
                           third-party  domain=a.com|~b.com  match-case  (and ~ negations of the types)
  element hiding  ##selector  domain.com##selector  #@#selector (exceptions)  $elemhide / $generichide / $document
Rules that need features we do not have (procedural selectors such as :has-text(), redirect, csp, removeparam, ...)
are skipped rather than applied wrongly.

Settings (config.py, all optional):
  ADBLOCK                 True to block ads and trackers (default False)
  ADBLOCK_LISTS           filter list URLs or file paths (default: EasyList, EasyPrivacy, Peter Lowe's list)
  ADBLOCK_UPDATE_HOURS    how often to refresh downloaded lists (default 24)
  ADBLOCK_CACHE_DIR       where downloaded lists are kept (default /app/data/adblock if /app/data exists, else a temp dir)
  ADBLOCK_ALLOWLIST       domains that are never blocked, and whose pages are never filtered
  ADBLOCK_CUSTOM_RULES    extra rules in filter-list syntax, e.g. ["||ads.example.com^", "example.com##.promo"]
  ADBLOCK_COSMETIC        True (default) to remove ad elements from pages, False to only block requests
"""

DEFAULT_LISTS = [
	"https://easylist.to/easylist/easylist.txt",
	"https://easylist.to/easylist/easyprivacy.txt",
	"https://pgl.yoyo.org/adservers/serverlist.php?hostformat=hosts&showintro=0&mimetype=plaintext",
]

# Used until the real lists have been downloaded (or when there is no network)
BUILTIN_RULES = """
||doubleclick.net^
||googlesyndication.com^
||googleadservices.com^
||googletagservices.com^
||googletagmanager.com^
||google-analytics.com^
||adservice.google.com^
||pagead2.googlesyndication.com^
||2mdn.net^
||amazon-adsystem.com^
||adnxs.com^
||adsrvr.org^
||advertising.com^
||casalemedia.com^
||criteo.com^
||criteo.net^
||moatads.com^
||openx.net^
||outbrain.com^
||taboola.com^
||pubmatic.com^
||quantserve.com^
||rubiconproject.com^
||scorecardresearch.com^
||smartadserver.com^
||yieldmo.com^
||zedo.com^
##.adsbygoogle
##ins.adsbygoogle
##.ad-banner
##.ad-container
##.advertisement
##[id^="google_ads"]
##[id^="div-gpt-ad"]
##iframe[src*="doubleclick.net"]
##iframe[src*="googlesyndication.com"]
"""

MAX_LIST_BYTES = 30 * 1024 * 1024
TYPES = {"script", "image", "stylesheet", "object", "xmlhttprequest", "subdocument", "document", "font", "media", "ping", "other"}
# uBlock aliases we do understand (any other option that is not listed below makes the rule be skipped)
OPTION_ALIASES = {"xhr": "xmlhttprequest", "frame": "subdocument", "1p": "~third-party", "first-party": "~third-party",
				  "3p": "third-party", "css": "stylesheet"}
# Element-hiding selectors that need a procedural engine we do not have
PROCEDURAL_MARKERS = (":has-text(", ":-abp-", ":xpath(", ":matches-css", ":matches-attr", ":matches-media", ":matches-path", ":style(",
					  ":remove(", ":upward(", ":nth-ancestor(", ":min-text-length(", ":watch-attr(", ":contains(", ":not-has(",
					  ":if(", ":if-not(", ":others(", ":-js(", "+js(")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9\-_.]*[a-z0-9])?$")
TOKEN_RE = re.compile(r"[a-z0-9%]{3,}")
TWO_LEVEL_FALLBACK = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "co.nz", "co.jp", "com.br", "co.in", "com.cn", "co.za"}

# Responses used in place of a blocked request
EMPTY_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00"
			 b"\x02\x02D\x01\x00;")


# ----------------------------------------------------------------------------------
# URL helpers
# ----------------------------------------------------------------------------------

try:
	from publicsuffix2 import get_sld as _get_sld
except ImportError:  # pragma: no cover - the package is in requirements.txt
	_get_sld = None


def registrable_domain(host):
	"""The part of a host that identifies a site (example.com, bbc.co.uk); an IP address is its own site."""
	host = (host or "").lower().strip(".")
	if not host:
		return ""
	try:
		ipaddress.ip_address(host.strip("[]"))
		return host
	except ValueError:
		pass
	parts = host.split(".")
	if _get_sld is not None:
		sld = _get_sld(host) or host
		# A TLD missing from the public-suffix list (.test, .lan, ...) comes back as just that TLD; use the last two labels
		return sld if "." in sld or len(parts) < 2 else ".".join(parts[-2:])
	if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_FALLBACK:
		return ".".join(parts[-3:])
	return ".".join(parts[-2:])


def is_third_party(request_host, page_host):
	if not page_host or not request_host:
		return False
	return registrable_domain(request_host) != registrable_domain(page_host)


def host_and_parents(host):
	"""example.com -> ['a.b.example.com', 'b.example.com', 'example.com', 'com']"""
	parts = host.split(".")
	return [".".join(parts[i:]) for i in range(len(parts))]


def domain_in(host, domains):
	return any(d in domains for d in host_and_parents(host))


def guess_request_type(url, accept="", has_referer=True):
	"""Best guess of what a browser request is for, from its URL and Accept header (a plain proxy sees no more)."""
	path = urlparse(url).path.lower()
	ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
	accept = (accept or "").lower()
	if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg", "avif", "tif", "tiff") or accept.startswith("image/"):
		return "image"
	if ext == "css" or accept.startswith("text/css"):
		return "stylesheet"
	if ext in ("js", "mjs"):
		return "script"
	if ext in ("woff", "woff2", "ttf", "otf", "eot"):
		return "font"
	if ext in ("mp4", "webm", "ogg", "mp3", "wav", "m4a", "mov", "flv", "swf"):
		return "media"
	if "text/html" in accept or "application/xhtml" in accept or ext in ("html", "htm", "php", "asp", "aspx", ""):
		return "subdocument" if has_referer else "document"
	return "other"


# ----------------------------------------------------------------------------------
# Rules
# ----------------------------------------------------------------------------------

class Rule:
	__slots__ = ("raw", "regex", "rest", "types", "not_types", "third", "domains", "not_domains", "match_case")

	def __init__(self, raw):
		self.raw = raw
		self.regex = None        # compiled pattern for the whole URL (general rules)
		self.rest = None         # compiled pattern for what follows the host (domain-anchored rules)
		self.types = None        # set of request types the rule is limited to, or None for all
		self.not_types = None
		self.third = None        # True: third-party only, False: first-party only, None: either
		self.domains = None      # pages the rule applies on
		self.not_domains = None
		self.match_case = False

	def applies(self, rtype, third, page_host):
		if self.types is not None and rtype not in self.types:
			return False
		if self.not_types is not None and rtype in self.not_types:
			return False
		if self.third is not None and third != self.third:
			return False
		if self.domains is not None or self.not_domains is not None:
			ph = page_host or ""
			if self.not_domains and domain_in(ph, self.not_domains):
				return False
			if self.domains is not None and not domain_in(ph, self.domains):
				return False
		return True


def pattern_to_regex(pattern):
	"""An Adblock pattern (with * wildcards, ^ separators and | anchors) as regular-expression source."""
	start = pattern.startswith("|")
	end = pattern.endswith("|") and len(pattern) > 1
	body = pattern[1 if start else 0: -1 if end else None]
	out = []
	for ch in body:
		if ch == "*":
			out.append(".*")
		elif ch == "^":
			out.append(r"(?:[^\w\-.%]|$)")
		else:
			out.append(re.escape(ch))
	src = "".join(out)
	return ("^" if start else "") + src + ("$" if end else "")


def best_token(pattern):
	"""The longest alphanumeric run that is bounded by separators in the pattern, used to index general rules."""
	best = None
	for m in TOKEN_RE.finditer(pattern):
		before = pattern[m.start() - 1] if m.start() > 0 else None
		after = pattern[m.end()] if m.end() < len(pattern) else None
		if before == "*" or after == "*":
			continue
		# A run at either edge is only safe if the pattern is anchored there
		if before is None and not pattern.startswith("|"):
			continue
		if after is None and not pattern.endswith("|"):
			continue
		if best is None or len(m.group(0)) > len(best):
			best = m.group(0)
	return best


def parse_options(text):
	"""Parse the part after $. Returns (settings dict) or None if the rule uses options we do not support."""
	types, not_types, third = set(), set(), None
	domains, not_domains = None, None
	match_case = False
	flags = set()
	for raw in text.split(","):
		opt = raw.strip().lower()
		if not opt:
			continue
		neg = opt.startswith("~")
		name = opt[1:] if neg else opt
		value = None
		if "=" in name:
			name, value = name.split("=", 1)
		name = OPTION_ALIASES.get(name, name)
		if name.startswith("~"):  # alias that expanded to a negation, e.g. 1p -> ~third-party
			neg, name = not neg, name[1:]
		if name == "third-party":
			third = (not neg)
		elif name == "match-case":
			match_case = True
		elif name == "domain":
			for d in (value or "").replace("|", ",").split(","):
				d = d.strip()
				if not d:
					continue
				if d.startswith("~"):
					not_domains = (not_domains or set()) | {d[1:]}
				else:
					domains = (domains or set()) | {d}
		elif name in ("important", "specificblock", "specifichide"):
			pass
		elif name in ("elemhide", "generichide", "document", "genericblock"):
			flags.add(name)
			if name == "document" and not neg:
				types.add("document")
		elif name in TYPES:
			(not_types if neg else types).add(name)
		else:
			return None  # redirect, csp, removeparam, popup, ...: not something we can honour
	return {"types": types or None, "not_types": not_types or None, "third": third, "domains": domains,
			"not_domains": not_domains, "match_case": match_case, "flags": flags}


# ----------------------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------------------

class Engine:
	def __init__(self):
		self.domain_rules = {}      # host -> [Rule] for ||host... rules
		self.domain_exceptions = {}
		self.token_rules = {}       # token -> [Rule] for the other rules
		self.token_exceptions = {}
		self.misc_rules = []        # rules with no usable token (scanned every time; there are few)
		self.misc_exceptions = []
		self.generic_hide = []      # selectors that apply on every site: (selector, excluded domains or None)
		self.domain_hide = {}       # domain -> [selector]
		self.hide_exceptions = {}   # domain -> {selector} (#@#)
		self.generic_hide_exceptions = set()
		self.no_cosmetic = set()    # domains where $elemhide / $document exceptions switch element hiding off
		self.no_generic_cosmetic = set()  # domains with $generichide
		self.allow_pages = set()    # domains where $document exceptions switch all blocking off

	@property
	def size(self):
		network = sum(len(v) for v in self.domain_rules.values()) + sum(len(v) for v in self.token_rules.values()) + len(self.misc_rules)
		return {"network_rules": network,
				"exceptions": sum(len(v) for v in self.domain_exceptions.values()) + sum(len(v) for v in self.token_exceptions.values()) + len(self.misc_exceptions),
				"hide_rules": len(self.generic_hide) + sum(len(v) for v in self.domain_hide.values())}

	# -- loading ----------------------------------------------------------------

	def add_text(self, text):
		for line in text.splitlines():
			self.add_line(line)

	def add_line(self, line):
		line = line.strip()
		if not line or line[0] in "![":
			return
		if line[0] == "#" and not line.startswith(("##", "#@#", "#?#", "#$#", "#%#", "#@?#", "#@$#", "#@%#")):
			return  # a comment in a hosts file
		if self._hosts_line(line):
			return
		if "#?#" in line or "#$#" in line or "#%#" in line or "#@?#" in line or "#@$#" in line or "#@%#" in line:
			return  # procedural / scriptlet rules
		if "##" in line or "#@#" in line:
			return self._cosmetic(line)
		self._network(line)

	def _hosts_line(self, line):
		"""'0.0.0.0 ads.example.com' (hosts-file format) or a bare domain on its own line."""
		parts = line.split("#", 1)[0].split()
		if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1", "::1", "::", "0"):
			for host in parts[1:]:
				host = host.lower()
				if host not in ("localhost", "local", "broadcasthost", "ip6-localhost", "ip6-loopback", "0.0.0.0") and HOST_RE.match(host) and "." in host:
					rule = Rule(f"||{host}^")
					rule.rest = None
					self.domain_rules.setdefault(host, []).append(rule)
			return True
		return False

	def _cosmetic(self, line):
		exception = "#@#" in line
		sep = "#@#" if exception else "##"
		domains_part, _, selector = line.partition(sep)
		selector = selector.strip()
		if not selector or selector.startswith("^") or any(m in selector for m in PROCEDURAL_MARKERS):
			return
		included, excluded = set(), set()
		for d in domains_part.split(","):
			d = d.strip().lower()
			if not d:
				continue
			if d.startswith("~"):
				excluded.add(d[1:])
			else:
				included.add(d)
		if exception:
			if included:
				for d in included:
					self.hide_exceptions.setdefault(d, set()).add(selector)
			else:
				self.generic_hide_exceptions.add(selector)
			return
		if included:
			for d in included:
				self.domain_hide.setdefault(d, []).append(selector)
		else:
			self.generic_hide.append((selector, frozenset(excluded) if excluded else None))

	def _network(self, line):
		exception = line.startswith("@@")
		if exception:
			line = line[2:]
		pattern, opts_text = line, ""
		if "$" in line and not (line.startswith("/") and line.endswith("/") and len(line) > 1):
			idx = line.rfind("$")
			maybe = line[idx + 1:]
			if re.fullmatch(r"[\w\-~=|.,*:/]+", maybe or "") and not maybe.startswith("/"):
				pattern, opts_text = line[:idx], maybe
		opts = parse_options(opts_text) if opts_text else {"types": None, "not_types": None, "third": None, "domains": None,
															"not_domains": None, "match_case": False, "flags": set()}
		if opts is None or not pattern:
			return
		rule = Rule(line)
		rule.types, rule.not_types, rule.third = opts["types"], opts["not_types"], opts["third"]
		rule.domains, rule.not_domains, rule.match_case = opts["domains"], opts["not_domains"], opts["match_case"]
		flags = re.IGNORECASE if not rule.match_case else 0

		# Exceptions that switch blocking or element hiding off for a whole site
		if exception and ({"elemhide", "document", "generichide"} & opts["flags"]) and pattern.startswith("||"):
			host = re.split(r"[\^/*?|:]", pattern[2:], maxsplit=1)[0].lower()
			if HOST_RE.match(host):
				if "generichide" in opts["flags"]:
					self.no_generic_cosmetic.add(host)
				else:
					self.no_cosmetic.add(host)
					if "document" in opts["flags"]:
						self.allow_pages.add(host)
			return
		if opts["flags"] & {"elemhide", "generichide", "genericblock"}:
			return

		table_domain = self.domain_exceptions if exception else self.domain_rules
		table_token = self.token_exceptions if exception else self.token_rules
		misc = self.misc_exceptions if exception else self.misc_rules

		try:
			if len(pattern) > 2 and pattern.startswith("/") and pattern.endswith("/"):
				rule.regex = re.compile(pattern[1:-1], flags)
				return misc.append(rule)
			if pattern.startswith("||"):
				body = pattern[2:]
				host = re.split(r"[\^/*?|:]", body, maxsplit=1)[0].lower()
				rest = body[len(host):]
				# "||ad*.example.com^" has a wildcard inside the host: it cannot be indexed by host
				wildcard_in_host = rest.startswith("*") and "." in re.match(r"\*[^\^/?|]*", rest).group(0)
				if host and HOST_RE.match(host) and not wildcard_in_host and (rest == "" or rest[0] in "^/?*:"):
					# ||host^ blocks the whole host; anything after the host is matched against what follows it in the URL
					rule.rest = None if rest in ("", "^", "^|") else re.compile(pattern_to_regex(rest), flags)
					return table_domain.setdefault(host, []).append(rule)
				# ||ad*.example.com/...: anchored at the start of a host, but not a plain host we can index
				rule.regex = re.compile(r"^[\w+.\-]+://([^/?#]*\.)?" + pattern_to_regex(body), flags)
				return misc.append(rule)
			rule.regex = re.compile(pattern_to_regex(pattern), flags)
		except re.error:
			return
		token = best_token(pattern.lower())
		if token:
			table_token.setdefault(token, []).append(rule)
		else:
			misc.append(rule)

	# -- matching ----------------------------------------------------------------

	@staticmethod
	def _rule_matches(rule, url, url_l, remainder, rtype, third, page_host):
		if not rule.applies(rtype, third, page_host):
			return False
		if rule.regex is not None:
			return rule.regex.search(url if rule.match_case else url_l) is not None
		if rule.rest is not None:
			return rule.rest.match(remainder if rule.match_case else remainder.lower()) is not None
		return True

	def _find(self, domain_table, token_table, misc, url, url_l, host, remainder, rtype, third, page_host):
		for h in host_and_parents(host):
			for rule in domain_table.get(h, ()):
				if self._rule_matches(rule, url, url_l, remainder, rtype, third, page_host):
					return rule
		for token in set(TOKEN_RE.findall(url_l)):
			for rule in token_table.get(token, ()):
				if self._rule_matches(rule, url, url_l, remainder, rtype, third, page_host):
					return rule
		for rule in misc:
			if self._rule_matches(rule, url, url_l, remainder, rtype, third, page_host):
				return rule
		return None

	def match(self, url, rtype="other", page_host=None):
		"""The raw text of the rule that blocks this request, or None if it may proceed."""
		parsed = urlparse(url)
		host = (parsed.hostname or "").lower()
		if not host or parsed.scheme not in ("http", "https", "ws", "wss", "ftp"):
			return None
		if page_host and domain_in(page_host.lower(), self.allow_pages):
			return None
		url_l = url.lower()
		third = is_third_party(host, page_host) if page_host else False
		idx = url_l.find(host)
		remainder = url[idx + len(host):] if idx >= 0 else ""
		rule = self._find(self.domain_rules, self.token_rules, self.misc_rules, url, url_l, host, remainder, rtype, third, page_host)
		if rule is None:
			return None
		exception = self._find(self.domain_exceptions, self.token_exceptions, self.misc_exceptions, url, url_l, host, remainder, rtype, third, page_host)
		return None if exception is not None else rule.raw

	# -- element hiding ----------------------------------------------------------

	def matcher(self, selector):
		"""A compiled matcher for a selector (cached), or None if the selector cannot be parsed."""
		cache = self.__dict__.setdefault("_matchers", {})
		if selector not in cache:
			try:
				cache[selector] = compile_selector(selector)
			except Exception:
				cache[selector] = None
		return cache[selector]

	def selectors_for(self, page_host):
		"""CSS selectors of elements to remove on a page of `page_host`."""
		page_host = (page_host or "").lower()
		if domain_in(page_host, self.no_cosmetic):
			return []
		excepted = set(self.generic_hide_exceptions)
		for h in host_and_parents(page_host):
			excepted |= self.hide_exceptions.get(h, set())
		selectors = []
		if not domain_in(page_host, self.no_generic_cosmetic):
			for selector, excluded in self.generic_hide:
				if selector in excepted or (excluded and domain_in(page_host, excluded)):
					continue
				selectors.append(selector)
		for h in host_and_parents(page_host):
			for selector in self.domain_hide.get(h, ()):
				if selector not in excepted:
					selectors.append(selector)
		return selectors


def build_engine(texts):
	engine = Engine()
	for text in texts:
		engine.add_text(text)
	return engine


# ----------------------------------------------------------------------------------
# Fast element matching for element-hiding selectors
# ----------------------------------------------------------------------------------

_SIMPLE_SELECTOR = re.compile(r"^(?P<tag>\*|[a-zA-Z][\w-]*)?(?P<rest>(?:#[\w-]+|\.[\w-]+|\[[^\]]+\])*)$")
_SIMPLE_PART = re.compile(r"#([\w-]+)|\.([\w-]+)|\[\s*([\w-]+)\s*(?:([~|^$*]?=)\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\]]+))\s*([iIsS])?)?\s*\]")


def _attr_text(value):
	return " ".join(value) if isinstance(value, (list, tuple)) else (value if isinstance(value, str) else "")


def compile_selector(selector):
	"""A function el -> bool for the selector. Simple selectors (tag, #id, .class, [attr op value]) are evaluated directly in
	Python, which is about 25x faster than soupsieve; anything else (combinators, pseudo-classes) goes through soupsieve."""
	m = _SIMPLE_SELECTOR.match(selector)
	if m:
		tag = (m.group("tag") or "*").lower()
		checks = []
		ok = True
		for part in _SIMPLE_PART.finditer(m.group("rest")):
			ident, klass, attr, op, v1, v2, v3, flag = part.groups()
			if ident:
				checks.append(("id", ident))
			elif klass:
				checks.append(("class", klass))
			else:
				value = v1 if v1 is not None else v2 if v2 is not None else v3
				checks.append(("attr", attr.lower(), op, value, bool(flag and flag in "iI")))
		if ok:
			def simple(el):
				if tag != "*" and el.name != tag:
					return False
				attrs = el.attrs
				if attrs is None:
					return False
				for check in checks:
					kind = check[0]
					if kind == "id":
						if attrs.get("id") != check[1]:
							return False
					elif kind == "class":
						if check[1] not in (attrs.get("class") or ()):
							return False
					else:
						_, name, op, value, ci = check
						if name not in attrs:
							return False
						if op is None:
							continue
						have = _attr_text(attrs[name])
						if ci:
							have, value = have.lower(), value.lower()
						if op == "=":
							good = have == value
						elif op == "^=":
							good = bool(value) and have.startswith(value)
						elif op == "$=":
							good = bool(value) and have.endswith(value)
						elif op == "*=":
							good = bool(value) and value in have
						elif op == "~=":
							good = bool(value) and value in have.split()
						else:  # |=
							good = have == value or have.startswith(value + "-")
						if not good:
							return False
				return True
			return simple
	import soupsieve
	compiled = soupsieve.compile(selector)  # raises for selectors soupsieve cannot parse; callers skip those
	return compiled.match


class HideIndex:
	"""Finds the few elements an element-hiding selector could match, by class, id, tag or attribute name."""

	def __init__(self, elements):
		from utils.layout_utils import DocIndex, selector_tokens
		self._tokens = selector_tokens
		self.base = DocIndex(elements)
		self.all = self.base.all
		self.by_attr = {}
		for el in self.all:
			for name in (el.attrs or ()):
				self.by_attr.setdefault(name.lower(), []).append(el)
		self._attr_cache = {}

	def candidates(self, selector):
		found = self.base.candidates(selector)
		if found is not self.all:
			return found
		# No class, id or tag to go on: use the attributes named in the last compound selector
		names = self._attr_cache.get(selector)
		if names is None:
			last = re.split(r"[\s>+~]+(?![^\[]*\])", selector.strip())[-1]
			names = self._attr_cache[selector] = [n.lower() for n in re.findall(r"\[\s*([\w-]+)", last)]
		if names:
			return min((self.by_attr.get(n, []) for n in names), key=len)
		return found


# ----------------------------------------------------------------------------------
# Applying the engine to a page
# ----------------------------------------------------------------------------------

# Tags whose URL attribute loads a resource, and what kind of request that makes
URL_ATTRIBUTES = (
	("script", "src", "script"), ("iframe", "src", "subdocument"), ("frame", "src", "subdocument"), ("img", "src", "image"),
	("embed", "src", "object"), ("object", "data", "object"), ("source", "src", "media"), ("video", "src", "media"),
	("audio", "src", "media"), ("input", "src", "image"),
)


def apply_to_soup(soup, page_url, engine, cosmetic=True, allowlist=(), deadline=None):
	"""Remove ad elements from a parsed page. Returns {'requests': n, 'cosmetic': n}."""
	from bs4 import Tag

	result = {"requests": 0, "cosmetic": 0, "hosts": []}
	page_host = (urlparse(page_url).hostname or "").lower() if page_url else ""
	if page_host and domain_in(page_host, set(a.lower().lstrip(".") for a in allowlist)):
		return result
	deadline = deadline or (time.time() + 4.0)

	# 1. Tags that load a blocked URL
	for tag_name, attr, rtype in URL_ATTRIBUTES:
		for tag in soup.find_all(tag_name):
			if tag.decomposed:
				continue
			value = tag.get(attr)
			if not value or not isinstance(value, str) or value.startswith(("data:", "javascript:", "#")):
				continue
			target = urljoin(page_url, value) if page_url else value
			if engine.match(target, rtype, page_host):
				result["hosts"].append(urlparse(target).hostname or "?")
				tag.decompose()
				result["requests"] += 1
	for tag in soup.find_all("link"):
		if tag.decomposed:
			continue
		rel = [r.lower() for r in (tag.get("rel") or [])]
		href = tag.get("href")
		if href and ("stylesheet" in rel or "preload" in rel or "prefetch" in rel or "modulepreload" in rel):
			target = urljoin(page_url, href) if page_url else href
			rtype = "stylesheet" if "stylesheet" in rel else "script" if (tag.get("as") or "").lower() == "script" else "other"
			if engine.match(target, rtype, page_host):
				result["hosts"].append(urlparse(target).hostname or "?")
				tag.decompose()
				result["requests"] += 1

	# 2. Elements matched by element-hiding rules
	if cosmetic:
		selectors = engine.selectors_for(page_host)
		if selectors:
			body = soup.body or soup
			total_text = len(body.get_text(" ", strip=True)) or 1
			index = HideIndex(soup.find_all(True))
			doomed = {}
			for selector in selectors:
				if time.time() > deadline:
					break
				candidates = index.candidates(selector)
				if not candidates:
					continue
				matcher = engine.matcher(selector)
				if matcher is None:
					continue
				for el in candidates:
					if id(el) not in doomed and matcher(el):
						doomed[id(el)] = el
			for el in doomed.values():
				if not isinstance(el, Tag) or el.decomposed or el.name in ("html", "body", "head") or el.parent is None:
					continue
				# A bad generic rule must not be able to blank the page: leave elements that hold most of its text
				if len(el.get_text(" ", strip=True)) > max(1500, 0.5 * total_text):
					continue
				el.decompose()
				result["cosmetic"] += 1
	return result


def stub_response(url, rtype, accept=""):
	"""(body, content_type) to answer a blocked request with, chosen so the browser has nothing to complain about."""
	accept = (accept or "").lower()
	if rtype == "image" or accept.startswith("image/"):
		return EMPTY_GIF, "image/gif"
	if rtype == "stylesheet":
		return b"", "text/css"
	if rtype == "script":
		return b"", "application/javascript"
	if rtype in ("subdocument", "document"):
		return b"<html><body></body></html>", "text/html"
	return b"", "text/plain"


# ----------------------------------------------------------------------------------
# Lists: download, cache, refresh
# ----------------------------------------------------------------------------------

def _cache_name(source):
	import hashlib
	return hashlib.sha1(source.encode()).hexdigest()[:16] + ".txt"


def _looks_like_a_list(text):
	"""Reject HTML error pages and empty bodies that a server may return with status 200."""
	head = text[:2000].lower()
	if "<html" in head or "<!doctype" in head:
		return False
	return len([l for l in text.splitlines()[:400] if l.strip() and not l.startswith(("!", "#", "["))]) >= 5


class Manager:
	"""Owns the current Engine, keeps the downloaded lists fresh, and counts what was blocked."""

	def __init__(self, config):
		self.enabled = bool(getattr(config, "ADBLOCK", False))
		lists = getattr(config, "ADBLOCK_LISTS", None)
		self.sources = list(DEFAULT_LISTS if lists is None else lists)
		self.update_hours = float(getattr(config, "ADBLOCK_UPDATE_HOURS", None) or 24)
		self.allowlist = {d.lower().lstrip(".") for d in (getattr(config, "ADBLOCK_ALLOWLIST", None) or [])}
		self.custom_rules = list(getattr(config, "ADBLOCK_CUSTOM_RULES", None) or [])
		self.cosmetic = bool(getattr(config, "ADBLOCK_COSMETIC", True))
		self.skip_domains = set()  # set by the proxy: domains served by extensions, which are never filtered
		cache_dir = getattr(config, "ADBLOCK_CACHE_DIR", None)
		if not cache_dir:
			cache_dir = "/app/data/adblock" if os.path.isdir("/app/data") else os.path.join(tempfile.gettempdir(), "macproxy_adblock")
		self.cache_dir = cache_dir
		self.engine = build_engine([BUILTIN_RULES] + ["\n".join(self.custom_rules)])
		self.status = {}   # source -> {"rules": n, "updated": timestamp or None, "error": str or None}
		self.blocked = Counter()   # host -> requests blocked at the proxy / in the renderer
		self.total_blocked = 0
		self.cosmetic_removed = 0
		self._stop = threading.Event()
		self._lock = threading.Lock()

	# -- decisions -----------------------------------------------------------------

	def allowed(self, url, page_host=None):
		host = (urlparse(url).hostname or "").lower()
		exempt = self.allowlist | self.skip_domains
		return domain_in(host, exempt) or bool(page_host and domain_in(page_host.lower(), exempt))

	def should_block(self, url, rtype, page_host=None):
		"""The matching rule if this request should be blocked, else None. Counts what it blocks."""
		if not self.enabled or self.allowed(url, page_host):
			return None
		rule = self.engine.match(url, rtype, page_host)
		if rule:
			host = (urlparse(url).hostname or "?").lower()
			with self._lock:
				self.blocked[host] += 1
				self.total_blocked += 1
		return rule

	def filter_page(self, soup, page_url):
		if not self.enabled:
			return {"requests": 0, "cosmetic": 0, "hosts": []}
		result = apply_to_soup(soup, page_url, self.engine, cosmetic=self.cosmetic, allowlist=self.allowlist | self.skip_domains)
		with self._lock:
			for host in result["hosts"]:
				self.blocked[host.lower()] += 1
			self.total_blocked += len(result["hosts"])
			self.cosmetic_removed += result["cosmetic"]
		return result

	# -- lists -----------------------------------------------------------------------

	def _path_for(self, source):
		return source if os.path.isfile(source) else os.path.join(self.cache_dir, _cache_name(source))

	def _read_cached(self, source):
		path = self._path_for(source)
		try:
			with open(path, encoding="utf-8", errors="replace") as f:
				return f.read(), os.path.getmtime(path)
		except OSError:
			return None, None

	def _download(self, source):
		resp = requests.get(source, timeout=(10, 60), headers={"User-Agent": "macproxy-plus adblock list updater"}, stream=True)
		resp.raise_for_status()
		data = resp.raw.read(MAX_LIST_BYTES + 1, decode_content=True)
		if len(data) > MAX_LIST_BYTES:
			raise ValueError("list is too large")
		text = data.decode("utf-8", errors="replace")
		if not _looks_like_a_list(text):
			raise ValueError("response does not look like a filter list")
		os.makedirs(self.cache_dir, exist_ok=True)
		fd, tmp = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
		with os.fdopen(fd, "w", encoding="utf-8") as f:
			f.write(text)
		os.replace(tmp, self._path_for(source))
		return text

	def rebuild(self, texts=None):
		"""Build a new engine from the cached lists (or the given texts) and swap it in."""
		if texts is None:
			texts = []
			for source in self.sources:
				text, mtime = self._read_cached(source)
				if text:
					texts.append(text)
					self.status[source] = {**self.status.get(source, {}), "rules": text.count("\n"), "updated": mtime}
		texts = [BUILTIN_RULES] + texts + ["\n".join(self.custom_rules)]
		started = time.time()
		engine = build_engine(texts)
		self.engine = engine
		print(f"Adblock: {engine.size['network_rules']} blocking rules, {engine.size['exceptions']} exceptions and "
			  f"{engine.size['hide_rules']} element rules loaded in {time.time() - started:.1f}s")

	def refresh(self, force=False):
		"""Download any list that is missing or older than ADBLOCK_UPDATE_HOURS, then rebuild. Returns how many were updated."""
		updated = 0
		for source in self.sources:
			if os.path.isfile(source):
				continue
			text, mtime = self._read_cached(source)
			stale = text is None or (time.time() - mtime) > self.update_hours * 3600
			if not (stale or force):
				continue
			try:
				self._download(source)
				updated += 1
				self.status[source] = {"updated": time.time(), "error": None}
			except Exception as e:
				print(f"Adblock: could not update {source}: {type(e).__name__}: {e}")
				self.status[source] = {**self.status.get(source, {}), "error": f"{type(e).__name__}: {e}"}
		if updated or force:
			self.rebuild()
		return updated

	def start(self):
		"""Load cached lists now, and keep them updated in the background."""
		if not self.enabled:
			return
		try:
			self.rebuild()
		except Exception as e:
			print(f"Adblock: could not load cached lists: {e}")

		def loop():
			while not self._stop.is_set():
				try:
					self.refresh()
				except Exception as e:
					print(f"Adblock: update failed: {e}")
				self._stop.wait(max(0.25, self.update_hours / 4) * 3600)

		threading.Thread(target=loop, name="adblock-updater", daemon=True).start()

	def stop(self):
		self._stop.set()


# ----------------------------------------------------------------------------------
# Module-level manager (set by the proxy at startup, used by the renderer)
# ----------------------------------------------------------------------------------

_manager = None


def init(config):
	global _manager
	_manager = Manager(config)
	_manager.start()
	return _manager


def get():
	return _manager
