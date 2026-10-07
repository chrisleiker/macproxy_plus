# Standard library imports
import copy
import hashlib
import html
import os
import re

# Third-party imports
from bs4 import BeautifulSoup
from bs4.formatter import HTMLFormatter
from flask import current_app, url_for

# First-party imports
from utils import adblock, aspect_utils, css_utils, image_scale, js_utils, layout_utils, site_overrides, svg_utils
from utils.image_utils import fetch_and_cache_image
from utils.system_utils import load_preset

# Get config
config = load_preset()


class URLAwareHTMLFormatter(HTMLFormatter):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)

	def escape(self, string):
		"""
		Escape special characters in the given string or list of strings.
		"""
		if isinstance(string, list):
			return [html.escape(str(item), quote=True) for item in string]
		elif string is None:
			return ''
		else:
			return html.escape(str(string), quote=True)

	def attributes(self, tag):
		for key, val in tag.attrs.items():
			if key in ['href', 'src']:  # Don't escape URL attributes
				yield key, val
			else:
				yield key, self.escape(val)

def transcode_content(content):
	"""
	Convert HTTPS to HTTP in CSS or JavaScript content
	"""
	if isinstance(content, bytes):
		content = content.decode('utf-8', errors='replace')
		
	# Simple pattern to match URLs in both CSS and JS
	patterns = [
		(r"""url\(['"]?(https://[^)'"]+)['"]?\)""", r"url(\1)"),  # CSS url()
		(r'"https://', '"http://'),  # Double-quoted URLs
		(r"'https://", "'http://"),  # Single-quoted URLs
		(r"https://", "http://"),    # Unquoted URLs
	]
	
	for pattern, replacement in patterns:
		content = re.sub(pattern, 
						lambda m: replacement.replace(r"\1", 
						m.group(1).replace("https://", "http://") if len(m.groups()) > 0 else ""),
						content)
	
	return content.encode('utf-8')

def downlevel_page_css(soup, url, page_style=None):
	"""Rewrite <style> blocks, style="" attributes and <link> tags for browsers with limited CSS support."""
	settings = css_utils.settings_from_config(config)
	site_vars = css_utils.get_site_vars(url)

	# Custom properties may be defined in a later <style> than the one using them, so collect first
	style_tags = soup.find_all('style')
	for tag in style_tags:
		if tag.string:
			try:
				css_utils.collect_vars(tag.string, settings, url, site_vars)
			except Exception as e:
				print(f"CSS variable collection failed: {e}")

	# Flexbox/grid emulation reads the original CSS, so it has to run before that CSS is translated
	if getattr(config, 'LAYOUT_EMULATION', False):
		layout_utils.emulate_layout(soup, url, settings, site_vars)

	for tag in style_tags:
		if not tag.string:
			continue
		if not css_utils.media_matches(tag.get('media'), settings):
			tag.decompose()
			continue
		try:
			tag.string = css_utils.downlevel_css(tag.string, settings, url, site_vars)
		except Exception as e:
			print(f"CSS downlevel failed for inline <style>: {e}")
			tag.decompose()

	for tag in soup.find_all(style=True):
		try:
			value = css_utils.downlevel_declarations(tag['style'], settings, site_vars)
		except Exception as e:
			print(f"CSS downlevel failed for style attribute: {e}")
			value = ''
		if value:
			tag['style'] = value
		else:
			del tag['style']

	for tag in soup.find_all('link'):
		rel = [r.lower() for r in (tag.get('rel') or [])]
		if 'stylesheet' in rel and 'alternate' not in rel:
			if not css_utils.media_matches(tag.get('media'), settings):
				tag.decompose()
				continue
			# The stylesheet is rewritten by the proxy, so integrity hashes would no longer match
			for attr in ('integrity', 'crossorigin', 'nonce'):
				tag.attrs.pop(attr, None)
		elif not any(r in ('icon', 'shortcut') for r in rel):
			# preload, prefetch, modulepreload, manifest, etc. are useless to an old browser
			tag.decompose()

	# Boxes that get their height from `aspect-ratio` collapse in a browser without it; give them one the old way
	if page_style is not None and aspect_utils.enabled(config):
		try:
			count = aspect_utils.emulate(soup, page_style, settings)
			if count:
				print(f"Aspect ratio: gave {count} boxes a height on {url[:80]}")
		except Exception as e:
			print(f"Aspect-ratio emulation failed ({type(e).__name__}: {e})")


def transcode_html(html, url=None, whitelisted_domains=None, simplify_html=False, 
				  tags_to_unwrap=None, tags_to_strip=None, attributes_to_strip=None,
				  convert_characters=False, conversion_table=None):
	"""
	Uses BeautifulSoup to transcode payloads of the text/html content type
	"""

	if isinstance(html, bytes):
		html = html.decode("utf-8", errors="replace")

	# Handle character conversion regardless of whitelist status
	if convert_characters:
		for key, replacement in conversion_table.items():
			if isinstance(replacement, bytes):
				replacement = replacement.decode("utf-8")
			html = html.replace(key, replacement)

	# The html5lib parser is required in order to preserve case-sensitivity of
	# tags. Using html.parser will corrupt SVGs and possibly other XML tags.
	soup = BeautifulSoup(html, "html5lib")

	# A <meta> content security policy makes the same demands as the header (see SECURITY_POLICY_HEADERS in proxy.py)
	for tag in soup.find_all('meta', attrs={'http-equiv': re.compile(r'^\s*(x-)?(webkit-)?content-security-policy(-report-only)?\s*$', re.I)}):
		tag.decompose()

	# Contents of <pre> tags should always use HTML entities
	for tag in soup.find_all(['pre']):
		tag.replace_with(str(tag))

	# Always convert HTTPS to HTTP regardless of whitelist status
	for tag in soup(['link', 'script', 'img', 'a', 'iframe', 'form']):
		# Handle src attributes
		if 'src' in tag.attrs:
			if tag['src'].startswith('https://'):
				tag['src'] = tag['src'].replace('https://', 'http://')
			elif tag['src'].startswith('//'):  # Handle protocol-relative URLs
				tag['src'] = 'http:' + tag['src']

		# Form actions (the browser would otherwise try to post straight to https://, which it cannot do through us)
		if 'action' in tag.attrs and isinstance(tag['action'], str):
			if tag['action'].startswith('https://'):
				tag['action'] = tag['action'].replace('https://', 'http://', 1)
			elif tag['action'].startswith('//'):
				tag['action'] = 'http:' + tag['action']

		# Handle href attributes
		if 'href' in tag.attrs:
			if tag['href'].startswith('https://'):
				tag['href'] = tag['href'].replace('https://', 'http://')
			elif tag['href'].startswith('//'):  # Handle protocol-relative URLs
				tag['href'] = 'http:' + tag['href']

	# Inline SVGs are drawn as pictures later on, and need the page's CSS to know their size and colour. Work that out now,
	# while the <style> and <link> tags are still in the page (the preset may strip them below).
	svg_style = page_style = None
	wants_svg = bool(getattr(config, 'SVG_STYLES', True) and url and soup.find('svg'))
	wants_aspect = bool(url and aspect_utils.enabled(config))
	if wants_svg or wants_aspect:
		try:
			page_style = layout_utils.StyleCascade(soup, url, css_utils.settings_from_config(config), css_utils.get_site_vars(url))
			if wants_svg:
				page_style.compute(svg_utils.elements_to_style(soup.find_all('svg')))
				svg_style = page_style
		except Exception as e:
			print(f"Page styles unavailable ({type(e).__name__}: {e}); drawing SVGs on their own, no aspect-ratio boxes")
			page_style = svg_style = None

	# Remove ads and trackers: tags that load a blocked URL, and elements the filter lists' hiding rules match
	ad_manager = adblock.get()
	if ad_manager is not None and ad_manager.enabled and url:
		removed = ad_manager.filter_page(soup, url)
		if removed['requests'] or removed['cosmetic']:
			print(f"Adblock: removed {removed['requests']} blocked tags and {removed['cosmetic']} ad elements from {url[:80]}")

	# Check if domain is whitelisted
	is_whitelisted = False
	if url:
		from urllib.parse import urlparse
		domain = urlparse(url).netloc
		is_whitelisted = any(domain.endswith(whitelisted) for whitelisted in whitelisted_domains)

	# Per-site special case: remove all JavaScript, whatever the preset or whitelist says
	if url and site_overrides.strips_javascript(url, config):
		js_utils.strip_javascript(soup)

	# Only perform tag/attribute stripping if the domain is not whitelisted and SIMPLIFY_HTML is True
	if simplify_html and not is_whitelisted:
		for tag in soup(tags_to_unwrap):
			tag.unwrap()
		for tag in soup(tags_to_strip):
			tag.decompose()
		for tag in soup():
			for attr in attributes_to_strip:
				if attr in tag.attrs:
					del tag[attr]

	# Translate CSS for older browsers (only when a preset/config opts in via CSS_MODE = "downlevel").
	# Sites on WHITELISTED_DOMAINS are left untouched here, like the other post-processing.
	if getattr(config, 'CSS_MODE', 'strip') == 'downlevel' and not is_whitelisted:
		downlevel_page_css(soup, url, page_style)

	# Always handle meta refresh tags
	for tag in soup.find_all('meta', attrs={'http-equiv': 'refresh'}):
		if 'content' in tag.attrs and 'https://' in tag['content']:
			tag['content'] = tag['content'].replace('https://', 'http://')

	# Always handle CSS with inline URLs
	for tag in soup.find_all(['style', 'link']):
		if tag.string:
			tag.string = tag.string.replace('https://', 'http://')

	# Handle inline SVGs - first pass
	# if any SVG has a child element containing <use href="#value"> or
	# <use xlink:href="#value"> then we need to find _another_ SVG on the page
	# with a child element containing <symbol id="value">, and replace the
	# contents of the first element with the contents of the second. If the
	# symbol tag defines a viewport, that viewport needs to be copied to the
	# parent of the use tag (which should be a svg tag)
	for use_tag in soup.find_all(['use']):
		attrs = use_tag.attrs
		if 'href' in attrs:
			attr = 'href'
		elif 'xlink:href' in attrs:
			attr = 'xlink:href'
		else:
			continue
		# Only same-document references to a <symbol> can be inlined. Leave anything else (a <use> pointing at a
		# plain shape, or at an external sprite sheet like "sprite.svg#icon") exactly as it is.
		if not use_tag[attr].startswith('#'):
			continue
		symbol_tag = soup.find("symbol", {"id": use_tag[attr][1:]})
		if symbol_tag is None:
			continue
		if 'viewBox' in symbol_tag.attrs and use_tag.parent.name == 'svg' and 'viewBox' not in use_tag.parent.attrs:
			use_tag.parent["viewBox"] = symbol_tag["viewBox"]
		symbol_tag_copy = copy.copy(symbol_tag)
		use_tag.replace_with(symbol_tag_copy)
		symbol_tag_copy.unwrap()

	# Handle inline SVGs - second pass
	# Fetch, cache, and convert them - then replace the inline <svg> tag with
	# an <img> tag whose src attribute points to this proxy _itself_.
	for tag in soup.find_all(['svg']):
		if tag.decomposed:
			continue
		prepared = None
		if svg_style is not None:
			try:
				prepared = svg_utils.prepare(tag, svg_style)
			except Exception as e:
				print(f"Could not prepare an SVG ({type(e).__name__}: {e}); drawing it on its own")
				prepared = False
			if prepared is None:
				tag.decompose()  # the page's CSS hides it (or it is only a sprite sheet)
				continue

		raster = None
		if prepared:
			markup, original = prepared.markup, prepared.attrs
			width, height, raster = prepared.width, prepared.height, prepared.raster
		else:
			# No page CSS to go on: set height and width equal to the viewport if one is not specified
			svg_attrs = tag.attrs
			if "height" not in svg_attrs and "viewBox" in svg_attrs:
				tag["height"] = svg_attrs["viewBox"].split(" ")[3]
			if "width" not in svg_attrs and "viewBox" in svg_attrs:
				tag["width"] = svg_attrs["viewBox"].split(" ")[2]
			markup, original = str(tag), {k: tag.get(k) for k in ("class", "id", "style", "title", "aria-label")}
			width, height = tag.get("width"), tag.get("height")

		# Convert it to a gif (or other specified format)
		fake_url = hashlib.md5(markup.encode()).hexdigest()
		convert = config.CONVERT_IMAGES
		convert_to = config.CONVERT_IMAGES_TO_FILETYPE
		cached = fetch_and_cache_image(
			fake_url,
			markup.encode('utf-8'),
			resize=config.RESIZE_IMAGES,
			max_width=config.MAX_IMAGE_WIDTH,
			max_height=config.MAX_IMAGE_HEIGHT,
			convert=convert,
			convert_to=convert_to,
			dithering=config.DITHERING_ALGORITHM,
			hash_url=False,
			scale_percent=getattr(config, 'IMAGE_SCALE_PERCENT', None),
			keep_alpha=True,
			always_process=True,
			svg_size=raster,
		)
		# The cache names the file after its real format, so use the name it returned
		cached_name = os.path.basename(cached) if cached else f"{fake_url}.gif"

		# The _external=True attribute of `url_for` doesn't work here, and will
		# always return `localhost` instead of our host IP / port. So grab that
		# info from the app config directly and prepend it to a relative URL instead.
		relative_url = url_for('serve_cached_image', filename=cached_name)
		img_url = f"http://{current_app.config['MACPROXY_HOST_AND_PORT']}{relative_url}"
		img_attrs = {"src": img_url}
		if height is not None:
			img_attrs["height"] = str(height)
		if width is not None:
			img_attrs["width"] = str(width)
		# Carry over what the page's CSS needs to keep placing and sizing it
		classes = original.get("class") or []
		classes = classes.split() if isinstance(classes, str) else list(classes)
		img_attrs["class"] = " ".join(classes + ["mp-svg"])
		if original.get("id"):
			img_attrs["id"] = original["id"]
		if original.get("style"):
			img_attrs["style"] = original["style"]
		label = original.get("aria-label") or original.get("title")
		img_attrs["alt"] = label if isinstance(label, str) else ""
		img = soup.new_tag("img", **img_attrs)
		tag.replace_with(img)

	# Images are shrunk by IMAGE_SCALE_PERCENT, so shrink their displayed size to match
	# (otherwise a width/height attribute would stretch the smaller file back up)
	scale = image_scale.normalize_percent(getattr(config, 'IMAGE_SCALE_PERCENT', None))
	if scale:
		image_scale.scale_img_tags(soup, scale)

	# Use the custom formatter when converting the soup back to a string
	html = soup.decode(formatter=URLAwareHTMLFormatter())

	html = html.replace('<br/>', '<br>')
	html = html.replace('<hr/>', '<hr>')
	
	# Ensure the output is properly encoded
	html_bytes = html.encode('utf-8')

	return html_bytes
