# Standard library imports
import argparse
import base64
import mimetypes
import os
import shutil
import socket
import threading
import time
from html import escape
from urllib.parse import urlparse, urlunparse

# Third-party imports
import requests
from flask import Flask, request, session, g, abort, Response, send_from_directory
from werkzeug.serving import get_interface_ip
from werkzeug.wrappers.response import Response as WerkzeugResponse

# First-party imports
from utils import adblock, cookie_utils, css_utils, js_utils, render_utils, site_overrides
from utils.html_utils import transcode_html, transcode_content
from utils.image_utils import is_image_url, fetch_and_cache_image, CACHE_DIR
from utils.system_utils import load_preset


os.environ['FLASK_ENV'] = 'development'
app = Flask(__name__)
# Each client gets its own server-side cookie jar (see utils/cookie_utils.py); there is no global session

HTTP_ERRORS = (403, 404, 500, 503, 504)
ERROR_HEADER = "[[Macproxy Encountered an Error]]"

# Global variable to store the override extension
override_extension = None

# User-Agent string
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"

# Call this function every time the proxy starts
def clear_image_cache():
	if os.path.exists(CACHE_DIR):
		shutil.rmtree(CACHE_DIR)
	os.makedirs(CACHE_DIR, exist_ok=True)

clear_image_cache()

# Load preset immediately after config import
config = load_preset()

# Now get the settings we need after preset has potentially modified them
ENABLED_EXTENSIONS = config.ENABLED_EXTENSIONS

# Per-client cookie jars, shared by the proxy's own requests and the headless browser
cookie_store = cookie_utils.store_from_config(config)

# Ad and tracker blocking (does nothing unless ADBLOCK = True); the lists load in the background
adblock_manager = adblock.init(config)

# Start headless Chromium in the background if JavaScript rendering is on, so the first page is not slow
render_utils.warm_up(config)

# Load extensions
extensions = {}
domain_to_extension = {}
print('Enabled Extensions: ')
for ext in ENABLED_EXTENSIONS:
	print(ext)
	try:
		module = __import__(f"extensions.{ext}.{ext}", fromlist=[''])
	except AttributeError as e:
		print(f"ERROR: extension '{ext}' could not load: {e}")
		print("Check that config.py defines every API key / setting this extension needs (see config.py.example).")
		raise SystemExit(1)
	extensions[ext] = module
	domain_to_extension[module.DOMAIN] = module
# Extensions generate their own pages, which the ad filter must leave alone
adblock_manager.skip_domains = set(domain_to_extension)

def image_mimetype(filename):
	# Cached images may be gif, png, jpeg, etc. depending on CONVERT_IMAGES_TO_FILETYPE
	return mimetypes.guess_type(filename)[0] or 'image/gif'

@app.route("/cached_image/<path:filename>")
def serve_cached_image(filename):
	return send_from_directory(CACHE_DIR, filename, mimetype=image_mimetype(filename))

def image_options():
	# Image settings from config (after any preset has been applied), as fetch_and_cache_image keyword arguments
	return dict(
		resize=config.RESIZE_IMAGES,
		max_width=config.MAX_IMAGE_WIDTH,
		max_height=config.MAX_IMAGE_HEIGHT,
		convert=config.CONVERT_IMAGES,
		convert_to=config.CONVERT_IMAGES_TO_FILETYPE,
		dithering=config.DITHERING_ALGORITHM,
		scale_percent=getattr(config, 'IMAGE_SCALE_PERCENT', None)
	)

def handle_image_request(url):
	cached_url = fetch_and_cache_image(url, **image_options())
	if cached_url:
		return send_from_directory(CACHE_DIR, os.path.basename(cached_url), mimetype=image_mimetype(cached_url))
	else:
		return abort(404, "Image not found or could not be processed")

@app.route("/", defaults={"path": "/"}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def handle_request(path):
	global override_extension
	parsed_url = urlparse(request.url)
	scheme = parsed_url.scheme
	host = parsed_url.netloc.split(':')[0]  # Remove port if present
	
	if override_extension:
		print(f'Current override extension: {override_extension}')

	blocked_response = block_if_ad(host)
	if blocked_response is not None:
		return blocked_response

	override_response = handle_override_extension(scheme)
	if override_response is not None:
		return process_response(override_response, request.url)

	matching_extension = find_matching_extension(host)
	if matching_extension:
		response = handle_matching_extension(matching_extension)
		return process_response(response, request.url)
	
	# Only handle image requests here if we're not using an extension
	if is_image_url(request.url) and not (override_extension or matching_extension):
		return handle_image_request(request.url)

	return handle_default_request()

def handle_override_extension(scheme):
	global override_extension
	if override_extension:
		extension_name = override_extension.split('.')[-1]
		if extension_name in extensions:
			if scheme in ['http', 'https', 'ftp']:
				response = extensions[extension_name].handle_request(request)
				check_override_status(extension_name)
				return response
			else:
				print(f"Warning: Unsupported scheme '{scheme}' for override extension.")
		else:
			print(f"Warning: Override extension '{extension_name}' not found. Resetting override.")
			override_extension = None
	return None  # Return None if no override is active

def check_override_status(extension_name):
	global override_extension
	if hasattr(extensions[extension_name], 'get_override_status') and not extensions[extension_name].get_override_status():
		override_extension = None
		print("Override disabled")

def block_if_ad(host):
	"""Answer requests for ad and tracker URLs with an empty stand-in instead of fetching them (see utils/adblock.py)."""
	if not adblock_manager.enabled or find_matching_extension(host) or override_extension:
		return None
	accept = request.headers.get("Accept", "")
	referer = request.headers.get("Referer")
	# A page the person asked for directly (typed or bookmarked, so no referring page) is never blocked
	if not referer and ("text/html" in accept or not accept):
		return None
	page_host = (urlparse(referer).hostname or "").lower() if referer else None
	rtype = adblock.guess_request_type(request.url, accept, has_referer=bool(referer))
	rule = adblock_manager.should_block(request.url, rtype, page_host)
	if not rule:
		return None
	print(f"Adblock: blocked {request.url[:100]} ({rtype}) by {rule[:60]}")
	body, content_type = adblock.stub_response(request.url, rtype, accept)
	response = Response(body, status=200, mimetype=content_type.split(';')[0])
	response.headers["Content-Type"] = content_type
	response.headers["X-Macproxy-Blocked"] = rule[:100].replace("\n", " ")
	response.headers["Cache-Control"] = "max-age=3600"
	return response

def find_matching_extension(host):
	for domain, extension in domain_to_extension.items():
		if host.endswith(domain):
			return extension
	return None

def handle_matching_extension(matching_extension):
	global override_extension
	print(f"Handling request with matching extension: {matching_extension.__name__}")
	response = matching_extension.handle_request(request)
	
	if hasattr(matching_extension, 'get_override_status') and matching_extension.get_override_status():
		override_extension = matching_extension.__name__
		print(f"Override enabled for {override_extension}")
	
	return response

def process_response(response, url):
	print(f"Processing response for URL: {url}")

	if isinstance(response, tuple):
		if len(response) == 3:
			content, status_code, headers = response
		elif len(response) == 2:
			content, status_code = response
			headers = {}
		else:
			content = response[0]
			status_code = 200
			headers = {}
	elif isinstance(response, (Response, WerkzeugResponse)):
		return response
	else:
		content = response
		status_code = 200
		headers = {}

	# Header names are case-insensitive (some servers send 'content-type' or 'Content-type')
	content_type = next((v for k, v in headers.items() if k.lower() == 'content-type'), '').lower()
	print(f"Content-Type: {content_type}")

	if content_type.startswith('image/'):
		# For image content, use the fetch_and_cache_image function with config values
		cached_url = fetch_and_cache_image(url, content, **image_options())
		if cached_url:
			return send_from_directory(CACHE_DIR, os.path.basename(cached_url), mimetype=image_mimetype(cached_url))
		else:
			return abort(404, "Image could not be processed")

	# Handle CSS and JavaScript (the media type may carry a "; charset=..." suffix)
	media_type = content_type.split(';')[0].strip()
	if media_type == 'text/css' and getattr(config, 'CSS_MODE', 'strip') == 'downlevel':
		try:
			css_settings = css_utils.settings_from_config(config)
			site_vars = css_utils.get_site_vars(url)
			text = css_utils.decode_css(content, content_type)
			css_utils.collect_vars(text, css_settings, url, site_vars)
			content = css_utils.downlevel_css(text, css_settings, url, site_vars).encode('utf-8')
		except Exception as e:
			# Fail open to the old behavior (an empty stylesheet) rather than serving CSS the browser may choke on
			print(f"CSS downlevel failed for {url}: {e}")
			content = b''
		response = Response(content, status_code)
		response.headers['Content-Type'] = 'text/css; charset=utf-8'
		return response
	if js_utils.is_javascript_content_type(media_type) and site_overrides.strips_javascript(url, config):
		# Site is configured to run no JavaScript: serve an empty script instead of the real one
		response = Response(b'', status_code)
		response.headers['Content-Type'] = content_type
		return response
	if media_type in ['text/css', 'text/javascript', 'application/javascript', 'application/x-javascript']:
		content = transcode_content(content)
		response = Response(content, status_code)
		response.headers['Content-Type'] = content_type
		return response

	# List of content types that should not be transcoded
	non_transcode_types = [
		'application/octet-stream',
		'application/pdf',
		'application/zip',
		'application/x-zip-compressed',
		'application/x-rar-compressed',
		'application/x-tar',
		'application/x-gzip',
		'application/x-bzip2',
		'application/x-7z-compressed',
		'application/mac-binary',
		'application/macbinary',
		'application/x-binary',
		'application/x-macbinary',
		'application/binhex',
		'application/binhex4',
		'application/mac-binhex',
		'application/mac-binhex40',
		'application/x-binhex40',
		'application/x-mac-binhex40',
		'application/x-sit',
		'application/x-stuffit',
		'application/vnd.openxmlformats-officedocument',
		'application/vnd.ms-excel',
		'application/vnd.ms-powerpoint',
		'application/msword',
		'audio/',
		'video/',
		'text/plain'
	]

	# Check if content type is in the list of non-transcode types
	should_transcode = not any(content_type.startswith(t) for t in non_transcode_types)
	# Only HTML is run through the HTML transcoder: JSON, XML, feeds and the like would be wrapped in <html><body>
	# tags and corrupted (application/xhtml+xml is still HTML)
	if media_type in ('application/json', 'application/xml', 'text/xml', 'text/csv') or (
			media_type.endswith(('+json', '+xml')) and media_type != 'application/xhtml+xml'):
		should_transcode = False

	if should_transcode:
		print("Transcoding content")
		if isinstance(content, bytes):
			content = content.decode('utf-8', errors='replace')
		content = transcode_html(
			content,
			url,
			whitelisted_domains=config.WHITELISTED_DOMAINS,
			simplify_html=config.SIMPLIFY_HTML,
			tags_to_unwrap=config.TAGS_TO_UNWRAP,
			tags_to_strip=config.TAGS_TO_STRIP,
			attributes_to_strip=config.ATTRIBUTES_TO_STRIP,
			convert_characters=config.CONVERT_CHARACTERS,
			conversion_table=config.CONVERSION_TABLE
		)
	else:
		print(f"Content type {content_type} should not be transcoded, passing through unchanged")

	response = Response(content, status_code)
	for key, value in headers.items():
		# Cookies stay on the server (cookie_store); the browser never sees them
		if key.lower() not in ["content-encoding", "content-length", "transfer-encoding", "set-cookie"] + SECURITY_POLICY_HEADERS:
			response.headers[key] = value

	print("Finished processing response")
	return response

# Headers that describe how the ORIGINAL site (served over https) wants its page treated. The proxy serves the page
# over http from a different address, so a browser that obeys them (PowerFox, Chrome, ...) would upgrade every request
# to https and block everything not on the page's own origin, and then fail to load any stylesheet or image.
SECURITY_POLICY_HEADERS = [
	"content-security-policy", "content-security-policy-report-only", "x-content-security-policy", "x-webkit-csp",
	"strict-transport-security", "upgrade-insecure-requests", "cross-origin-embedder-policy", "cross-origin-opener-policy",
	"cross-origin-resource-policy", "x-frame-options", "alt-svc", "expect-ct", "public-key-pins", "report-to", "nel",
]

def handle_default_request():
	url = request.url.replace("https://", "http://", 1)
	headers = prepare_headers()
	
	print(f"Handling default request for URL: {url}")
	
	try:
		resp = send_request(url, headers)
		content = resp.content
		status_code = resp.status_code
		headers = dict(resp.headers)
		content, headers = render_if_needed(resp, content, headers)
		return process_response((content, status_code, headers), url)
	except requests.exceptions.ConnectionError as e:
		error_args = str(e.args)
		if any(keyword in error_args for keyword in ["NameResolutionError", "nodename nor servname provided", "Failed to resolve"]):
			print(f"DNS lookup failed for {url}")
			return abort(502, f"DNS lookup failed for {url}. Please check the domain name.")
		else:
			print(f"Connection error for {url}: {str(e)}")
			return abort(502, f"Connection error: {str(e)}")
	except Exception as e:
		print(f"Error in handle_default_request: {str(e)}")
		return abort(500, ERROR_HEADER + str(e))

def cookies_enabled():
	"""Cookie and login support is off unless COOKIE_SUPPORT = True in config.py."""
	return cookie_utils.enabled(config)

def current_client():
	"""Identifies the cookie jar (and rendered-page cache) of the client making this request."""
	return cookie_utils.client_key(request.remote_addr, config)

def basic_auth_credentials():
	"""(username, password) from an HTTP Basic Authorization header, or None."""
	header = request.headers.get("Authorization", "")
	if header.lower().startswith("basic "):
		try:
			user, _, password = base64.b64decode(header[6:].strip()).decode("utf-8", "replace").partition(":")
			return user, password
		except Exception:
			return None
	return None

def render_if_needed(resp, content, headers):
	"""With RENDER_JAVASCRIPT on, replace an HTML page by its DOM after the page's scripts have run on the server."""
	# Only a page fetched with GET can be rendered. Check the request that produced the final response: after a login
	# POST that redirects to a dashboard it is a GET, while the result of a plain POST has no URL to load again.
	if resp.status_code >= 400 or getattr(resp.request, 'method', 'GET') != "GET":
		return content, headers
	media_type = next((v for k, v in headers.items() if k.lower() == 'content-type'), '').split(';')[0].strip().lower()
	if media_type not in ('text/html', 'application/xhtml+xml'):
		return content, headers
	# Render the final URL (after redirects), so the page sees the address the browser would end up on
	target = resp.url or request.url
	if not render_utils.should_render(target, config):
		return content, headers
	client = current_client()
	store, use_cache = cookie_store, True
	if not cookies_enabled():
		# Nothing is remembered between requests, but the redirect chain that led here (a login, a consent or session
		# cookie set on the way) must still be visible to the headless browser, or it would see a different page than
		# we just fetched. Give it this request's cookies only, in a throwaway store, and keep the result out of the cache.
		store = None
		collected = getattr(getattr(g, 'request_session', None), 'cookies', None)
		if collected is not None and len(collected):
			store = cookie_utils.CookieStore()
			for cookie in collected:
				store.jar(client).set_cookie(cookie)
			use_cache = False
	# A page produced by a form the client just submitted is shown first (once)
	html = render_utils.handoff_pop(client, target)
	if not html:
		html = render_utils.render_page(target, config, request.headers.get("Accept-Language"),
										client=client, store=store, auth=basic_auth_credentials(), use_cache=use_cache)
	if not html:
		return content, headers  # rendering failed: fall back to the page as the site sent it
	headers = {k: v for k, v in headers.items() if k.lower() not in ('content-type', 'content-length', 'content-encoding')}
	headers['Content-Type'] = 'text/html; charset=utf-8'
	return html.encode('utf-8'), headers

def prepare_headers():
	headers = {
		"Accept": request.headers.get("Accept"),
		"Accept-Language": request.headers.get("Accept-Language"),
		"Referer": request.headers.get("Referer"),
		"User-Agent": USER_AGENT,
	}
	# HTTP Basic/Digest credentials from the browser's own login prompt
	if request.headers.get("Authorization"):
		headers["Authorization"] = request.headers["Authorization"]
	return headers

REQUEST_TIMEOUT = (10, 60)  # connect, read (seconds)

def with_scheme(url, scheme):
	parts = urlparse(url)
	return urlunparse(parts._replace(scheme=scheme))

def request_body():
	"""The POST body as keyword arguments for requests: form fields (all values of each), uploaded files, or raw data."""
	if request.files:
		files = {name: (f.filename, f.stream.read(), f.mimetype) for name, f in request.files.items()}
		return {"data": request.form.to_dict(flat=False), "files": files}
	if request.form:
		return {"data": request.form.to_dict(flat=False)}
	raw = request.get_data()
	return {"data": raw} if raw else {}

def send_post(sess, url, headers):
	"""POST to the site over https first, so passwords never travel upstream unencrypted, falling back to http.

	Sites usually answer http:// with a 301 to https://, and a redirected POST is turned into a GET, which would
	silently drop a login. Posting to https:// directly avoids that."""
	targets = [with_scheme(url, "https"), url] if url.startswith("http://") else [url]
	body = request_body()
	last_error = None
	for target in targets:
		parts = urlparse(target)
		h = dict(headers)
		h["Origin"] = f"{parts.scheme}://{parts.netloc}"  # many sites reject a POST whose Origin/Referer is not theirs
		if h.get("Referer"):
			h["Referer"] = with_scheme(h["Referer"], parts.scheme)
		if request.content_type and "data" in body and isinstance(body["data"], bytes):
			h["Content-Type"] = request.content_type
		try:
			return sess.post(target, headers=h, allow_redirects=True, timeout=REQUEST_TIMEOUT, **body)
		except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
			last_error = e
	raise last_error

def send_request(url, headers):
	print(f"Sending request to: {url}")
	# Without cookie support each request gets a fresh session: cookies still work within one request (a redirect
	# chain) but nothing is remembered afterwards
	sess = cookie_store.session(current_client()) if cookies_enabled() else requests.Session()
	g.request_session = sess  # render_if_needed reads the cookies this request collected
	if request.method == "POST":
		return send_post(sess, url, headers)
	if headers.get("Authorization") and url.startswith("http://"):
		# Never send credentials over plain http if the site speaks https
		try:
			return sess.get(with_scheme(url, "https"), headers=headers, timeout=REQUEST_TIMEOUT)
		except (requests.exceptions.SSLError, requests.exceptions.ConnectionError):
			pass
	return sess.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

@app.after_request
def apply_caching(resp):
	try:
		resp.headers["Content-Type"] = g.content_type
	except:
		pass
	return resp

@app.after_request
def save_cookies(resp):
	# Writes the cookie jars to COOKIE_JAR_FILE when that is configured and something changed
	if cookies_enabled():
		cookie_store.save()
	return resp

@app.route("/__mp/form", methods=["POST"])
def handle_form_replay():
	"""Submit a form that the page's own scripts would have handled, by replaying it in the headless browser."""
	if not cookies_enabled():
		return abort(404)
	form = request.form
	page_url = form.get("__mp_url", "")
	if not render_utils.enabled(config) or not page_url:
		return abort(400, "Form replay is not available")
	# Only replay forms of the site the request is addressed to
	if urlparse(page_url).netloc.split(":")[0].lower() != request.host.split(":")[0].lower():
		return abort(400, "Form does not belong to this site")
	try:
		index = int(form.get("__mp_form", "0"))
	except ValueError:
		index = 0
	fields = [(name, value) for name, value in form.items(multi=True) if not name.startswith("__mp_")]
	result = render_utils.submit_form(page_url, index, fields, config,
									  accept_language=request.headers.get("Accept-Language"),
									  client=current_client(), store=cookie_store, auth=basic_auth_credentials())
	if not result:
		return abort(502, "The form could not be submitted")
	final_url, html = result
	render_utils.handoff_put(current_client(), final_url, html)
	# Show the result at its own address (so reloading and relative links behave), without a fragment
	location = with_scheme(final_url.split("#")[0], "http")
	return Response(status=303, headers={"Location": location})

def _adblock_page(message="", tested=""):
	m = adblock_manager
	size = m.engine.size
	def row(cells):
		return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
	if not m.enabled:
		body = "<p>Ad blocking is off. Set <code>ADBLOCK = True</code> in config.py to turn it on.</p>"
	else:
		lists = []
		for source in m.sources:
			st = m.status.get(source, {})
			updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(st["updated"])) if st.get("updated") else "not downloaded"
			lists.append(row([escape(source[:80]), st.get("rules", ""), updated, escape(st.get("error") or "")]))
		top = "".join(row([escape(h), n]) for h, n in m.blocked.most_common(25)) or row(["nothing blocked yet", ""])
		body = (f"<p>Blocking {size['network_rules']} request rules ({size['exceptions']} exceptions) and {size['hide_rules']} element rules.</p>"
				f"<p>Requests blocked: {m.total_blocked}. Elements removed from pages: {m.cosmetic_removed}.</p>"
				"<h2>Lists</h2><table border=\"1\" cellpadding=\"4\"><tr><th>List</th><th>Lines</th><th>Updated</th><th>Problem</th></tr>"
				+ "".join(lists) + "</table>"
				"<form method=\"post\" action=\"/__mp/adblock/refresh\"><input type=\"submit\" value=\"Update lists now\"></form>"
				"<h2>Most blocked hosts</h2><table border=\"1\" cellpadding=\"4\"><tr><th>Host</th><th>Blocked</th></tr>" + top + "</table>"
				"<h2>Test a URL</h2><form method=\"get\" action=\"/__mp/adblock\">URL: <input type=\"text\" name=\"url\" size=\"60\" value=\""
				+ escape(tested, quote=True) + "\"> Page it is on: <input type=\"text\" name=\"page\" size=\"20\"> <input type=\"submit\" value=\"Test\"></form>")
	return ("<html><head><title>Macproxy ad blocking</title></head><body><h1>Macproxy ad blocking</h1>" + message + body + "</body></html>")

@app.route("/__mp/adblock", methods=["GET"])
def adblock_status():
	tested = request.args.get("url", "").strip()
	message = ""
	if tested and adblock_manager.enabled:
		page_host = (urlparse(request.args.get("page", "").strip() if "//" in request.args.get("page", "") else "//" + request.args.get("page", "").strip()).hostname or "") or None
		rtype = adblock.guess_request_type(tested, "", has_referer=bool(page_host))
		rule = adblock_manager.engine.match(tested, rtype, page_host) if not adblock_manager.allowed(tested, page_host) else None
		message = (f"<p><b>{escape(tested)}</b> ({rtype}) would be <b>blocked</b> by <code>{escape(rule)}</code></p>" if rule
				   else f"<p><b>{escape(tested)}</b> ({rtype}) would <b>not</b> be blocked.</p>")
	return Response(_adblock_page(message, tested), mimetype="text/html")

@app.route("/__mp/adblock/refresh", methods=["POST"])
def adblock_refresh():
	if adblock_manager.enabled:
		threading.Thread(target=lambda: adblock_manager.refresh(force=True), daemon=True).start()
		message = "<p>Updating the lists in the background. Reload this page in a minute.</p>"
	else:
		message = ""
	return Response(_adblock_page(message), mimetype="text/html")

def _cookie_page(message=""):
	rows = []
	by_domain = {}
	for cookie in cookie_store.jar(current_client()):
		by_domain.setdefault(cookie.domain.lstrip("."), []).append(cookie.name)
	for domain in sorted(by_domain):
		names = ", ".join(sorted(set(by_domain[domain])))
		rows.append(f"<tr><td>{escape(domain)}</td><td>{len(by_domain[domain])}</td><td>{escape(names)}</td>"
					f"<td><form method=\"post\" action=\"/__mp/cookies/clear\"><input type=\"hidden\" name=\"domain\" value=\"{escape(domain, quote=True)}\">"
					f"<input type=\"submit\" value=\"Forget\"></form></td></tr>")
	table = ("<table border=\"1\" cellpadding=\"4\"><tr><th>Site</th><th>Cookies</th><th>Names (values are never shown)</th><th></th></tr>"
			 + "".join(rows) + "</table>") if rows else "<p>No cookies are stored for this device.</p>"
	return (f"<html><head><title>Macproxy cookies</title></head><body><h1>Macproxy cookies</h1>{message}"
			f"<p>Logins are kept on the proxy, not in your browser. Client: {escape(current_client())}</p>{table}"
			"<form method=\"post\" action=\"/__mp/cookies/clear\"><input type=\"submit\" value=\"Forget everything\"></form></body></html>")

@app.route("/__mp/cookies", methods=["GET"])
def show_cookies():
	if not cookies_enabled():
		return abort(404)
	return Response(_cookie_page(), mimetype="text/html")

@app.route("/__mp/cookies/clear", methods=["POST"])
def clear_cookies():
	if not cookies_enabled():
		return abort(404)
	domain = request.form.get("domain", "").strip().lower().lstrip(".")
	jar = cookie_store.jar(current_client())
	if domain:
		for cookie in list(jar):
			if cookie.domain.lstrip(".").lower() == domain:
				jar.clear(cookie.domain, cookie.path, cookie.name)
		message = f"<p>Forgot the cookies for {escape(domain)}.</p>"
	else:
		cookie_store.clear(current_client())
		message = "<p>Forgot all cookies for this device.</p>"
	return Response(_cookie_page(message), mimetype="text/html")

def get_proxy_hostname(hostname):
	# Based on the `log_startup` function from werkzeug.serving.
	# Translates a "bind all addresses" string into a real IP
	# (or returns the hostname if one was set)
	if hostname == "0.0.0.0":
		display_hostname = get_interface_ip(socket.AF_INET)
	elif hostname == "::":
		display_hostname = get_interface_ip(socket.AF_INET6)
	else:
		display_hostname = hostname
	return display_hostname

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Macproxy command line arguments")
	parser.add_argument(
		"--host",
		type=str,
		default="0.0.0.0",
		action="store",
		help="Host IP the web server will run on",
	)
	parser.add_argument(
		"--port",
		type=int,
		default=5001,
		action="store",
		help="Port number the web server will run on",
	)
	arguments = parser.parse_args()

	# Translate the bind address (typically 0.0.0.0 or ::) to a friendly
	# hostname / IP, and store it and the port in the application config
	# object. This will be used if we need to generate URLs to the proxy itself
	# in the HTML (as opposed to the site we are proxying the request to).
	app.config['MACPROXY_HOST_AND_PORT'] = f"{get_proxy_hostname(arguments.host)}:{arguments.port}"

	app.run(host=arguments.host, port=arguments.port, debug=False)
