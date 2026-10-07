# Standard library imports
import hashlib
import io
import mimetypes
import os
import tempfile

# Third-party imports
import requests
from PIL import Image, UnidentifiedImageError
from PILSVG import SVG

from utils.image_scale import normalize_percent, scaled_size


CACHE_DIR = os.path.join(os.path.dirname(__file__), "cached_images")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"

def get_svg_renderer():
	# If inkscape is installed and in the path, use that, because it supports
	# more SVG functionality. Otherwise, fall back to using skia.
	renderer='skia'
	if 'PATH' in os.environ:
		paths = os.environ['PATH'].split(':')
		for path in paths:
			exp_path = os.path.expandvars(os.path.join(path, 'inkscape'))
			if os.path.exists(exp_path):
				renderer='inkscape'
				break
	return renderer

# Pillow format names we can write back out, keyed by the spellings people use in config
SAVE_FORMATS = {"JPEG": "JPEG", "JPG": "JPEG", "PNG": "PNG", "GIF": "GIF", "WEBP": "WEBP", "BMP": "BMP", "TIFF": "TIFF"}
FORMAT_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "BMP": "bmp", "TIFF": "tiff"}


def sniff_extension(image_data, default="gif"):
	"""File extension for the real format of some image bytes."""
	try:
		return FORMAT_EXTENSIONS.get(Image.open(io.BytesIO(image_data)).format, default)
	except Exception:
		return default


def is_image_url(url):
	mime_type, _ = mimetypes.guess_type(url)
	return mime_type and mime_type.startswith('image/')

def optimize_image(image_data, resize=True, max_width=512, max_height=342, 
				  convert=True, convert_to='gif', dithering='FLOYDSTEINBERG', scale_percent=None, keep_alpha=False, svg_size=None):
	try:

		# Try to open the image directly using PIL
		# If this fails, assume we have an SVG, and try to open it using PILSVG.
		original_format = None
		try:
			img = Image.open(io.BytesIO(image_data))
			original_format = img.format
		except UnidentifiedImageError:
			# PILSVG doesn't support loading an image directly from a
			# byte stream, only from a file on disk. So create a temp file,
			# save the image data there, and then pass the path to PILSVG.
			with tempfile.NamedTemporaryFile(delete=False) as fp:
				try:
					fp.write(image_data)
					fp.close()
					svg = SVG(fp.name)
					# The renderer sizes an SVG from its viewBox; when we know the size it should be shown at, say so
					img = svg.im(size=[tuple(svg_size)], renderer=get_svg_renderer()) if svg_size else svg.im(renderer=get_svg_renderer())
				finally:
					fp.close()
					os.unlink(fp.name)

		# Convert RGBA images to RGB with white background, unless the caller wants transparency (an icon that is
		# white on a transparent background must stay that way) and the output format can hold it
		target = (convert_to or "png").lower() if convert else "png"
		alpha_kept = keep_alpha and target in ("png", "webp")
		if alpha_kept:
			if img.mode != 'RGBA':
				img = img.convert('RGBA')
		elif img.mode == 'RGBA':
			background = Image.new('RGB', img.size, (255, 255, 255))
			background.paste(img, mask=img.split()[3])
			img = background
		elif img.mode != 'RGB':
			img = img.convert('RGB')
		
		# Scale to a percentage of the original size (before the max-size cap below, which still applies)
		scale_percent = normalize_percent(scale_percent)
		if scale_percent:
			new_size = scaled_size(img.size[0], img.size[1], scale_percent)
			if new_size != img.size:
				img = img.resize(new_size, Image.Resampling.LANCZOS)

		# Resize if enabled and necessary
		if resize and max_width and max_height:
			width, height = img.size
			if width > max_width or height > max_height:
				ratio = min(max_width / width, max_height / height)
				new_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
				img = img.resize(new_size, Image.Resampling.LANCZOS)
		
		# Convert format if enabled
		if convert and convert_to:
			if convert_to.lower() == 'gif':
				# For black and white GIF
				img = img.convert("L")  # Convert to grayscale first
				dither_method = Image.Dither.FLOYDSTEINBERG if dithering and dithering.upper() == 'FLOYDSTEINBERG' else None
				img = img.convert("1", dither=dither_method)
			else:
				# For other format conversions
				img = img.convert(img.mode)
		
		output = io.BytesIO()
		# A resized image no longer carries its format, so remember the original one (SVGs are rendered to PNG)
		if convert and convert_to:
			save_format = SAVE_FORMATS.get(convert_to.upper(), convert_to.upper())
		else:
			save_format = original_format if original_format in SAVE_FORMATS.values() else "PNG"
		img.save(output, format=save_format, optimize=True)
		return output.getvalue()
		
	except Exception as e:
		print(f"Error optimizing image: {str(e)}")
		return image_data

def fetch_and_cache_image(url, content=None, resize=True, max_width=512, max_height=342,
						 convert=True, convert_to='gif', dithering='FLOYDSTEINBERG',
						 hash_url=True, scale_percent=None, keep_alpha=False, always_process=False, svg_size=None):
	try:
		print(f"Processing image: {url}")

		base_name = hashlib.md5(url.encode()).hexdigest() if hash_url else url
		converting = bool(convert and convert_to)
		if converting:
			# The output format is known up front, so the file name is too
			extension = SAVE_FORMATS.get(convert_to.upper(), convert_to.lower())
			extension = FORMAT_EXTENSIONS.get(extension, extension.lower())
			existing = os.path.join(CACHE_DIR, f"{base_name}.{extension}")
		else:
			# The format depends on the image itself, so look for any cached copy of this URL
			matches = [f for f in os.listdir(CACHE_DIR) if os.path.splitext(f)[0] == base_name]
			existing = os.path.join(CACHE_DIR, matches[0]) if matches else None
			extension = os.path.splitext(matches[0])[1].lstrip(".") if matches else None

		if not existing or not os.path.exists(existing):
			print(f"Optimizing and caching image: {url}")
			if content is None:
				response = requests.get(url, stream=True, headers={"User-Agent": USER_AGENT})
				response.raise_for_status()
				content = response.content

			# Only process if image conversion, resizing or scaling is enabled
			if convert or resize or always_process or normalize_percent(scale_percent):
				optimized_image = optimize_image(
					content,
					resize=resize,
					max_width=max_width,
					max_height=max_height,
					convert=convert,
					convert_to=convert_to,
					dithering=dithering,
					scale_percent=scale_percent,
					keep_alpha=keep_alpha,
					svg_size=svg_size
				)
			else:
				optimized_image = content

			if not converting:
				extension = sniff_extension(optimized_image)
			with open(os.path.join(CACHE_DIR, f"{base_name}.{extension}"), 'wb') as f:
				f.write(optimized_image)
		else:
			print(f"Image already cached: {url}")

		file_name = f"{base_name}.{extension}"
		cached_url = f"/cached_image/{file_name}"
		print(f"Cached URL: {cached_url}")
		return cached_url

	except Exception as e:
		print(f"Error processing image: {url}, Error: {str(e)}")
		return None

# Ensure cache directory exists
if not os.path.exists(CACHE_DIR):
	os.makedirs(CACHE_DIR)
