# Standard library imports
from urllib.parse import urlparse

"""
Per-site special cases. A site is matched by domain (the domain itself and any subdomain), and its
settings apply regardless of which preset is active.

Supported settings:
  strip_javascript  remove every trace of JavaScript from the site's pages and refuse its script files

Add or change entries in config.py:

	SITE_OVERRIDES = {
		"example.com": {"strip_javascript": True},
	}

Entries in config.py are merged over the built-in defaults below; set a site's value to {} (or
"strip_javascript": False) to turn a built-in default off.
"""

DEFAULT_SITE_OVERRIDES = {
	"theverge.com": {"strip_javascript": True},
}


def _domain_matches(host, domain):
	host, domain = host.lower().split(":")[0], domain.lower().lstrip(".")
	return host == domain or host.endswith("." + domain)


def get_overrides(url, config=None):
	"""Merged settings for the site `url` belongs to ({} if it has no overrides)."""
	if not url:
		return {}
	host = urlparse(url).netloc
	sources = [DEFAULT_SITE_OVERRIDES, getattr(config, "SITE_OVERRIDES", None) or {}]
	merged = {}
	for source in sources:
		# Longer (more specific) domains are applied last so they win
		for domain in sorted(source, key=len):
			if _domain_matches(host, domain):
				merged.update(source[domain])
	return merged


def strips_javascript(url, config=None):
	return bool(get_overrides(url, config).get("strip_javascript"))
