# Standard library imports
import argparse
import mimetypes
import os
import shutil
import socket
from urllib.parse import urlparse

# Third-party imports
import requests
from flask import Flask, request, session, g, abort, Response, send_from_directory
from werkzeug.serving import get_interface_ip
from werkzeug.wrappers.response import Response as WerkzeugResponse

# First-party imports
from utils import css_utils, js_utils, render_utils, site_overrides
from utils.html_utils import transcode_html, transcode_content
from utils.image_utils import is_image_url, fetch_and_cache_image, CACHE_DIR
from utils.system_utils import load_preset


os.environ['FLASK_ENV'] = 'development'
app = Flask(__name__)
session = requests.Session()

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
		if key.lower() not in ["content-encoding", "content-length", "transfer-encoding"]:
			response.headers[key] = value

	print("Finished processing response")
	return response

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

def render_if_needed(resp, content, headers):
	"""With RENDER_JAVASCRIPT on, replace an HTML page by its DOM after the page's scripts have run on the server."""
	if request.method != "GET" or resp.status_code >= 400:
		return content, headers
	media_type = next((v for k, v in headers.items() if k.lower() == 'content-type'), '').split(';')[0].strip().lower()
	if media_type not in ('text/html', 'application/xhtml+xml'):
		return content, headers
	# Render the final URL (after redirects), so the page sees the address the browser would end up on
	target = resp.url or request.url
	if not render_utils.should_render(target, config):
		return content, headers
	html = render_utils.render_page(target, config, request.headers.get("Accept-Language"))
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
	return headers

def send_request(url, headers):
	print(f"Sending request to: {url}")
	if request.method == "POST":
		return session.post(url, data=request.form, headers=headers, allow_redirects=True)
	else:
		return session.get(url, params=request.args, headers=headers)

@app.after_request
def apply_caching(resp):
	try:
		resp.headers["Content-Type"] = g.content_type
	except:
		pass
	return resp

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
