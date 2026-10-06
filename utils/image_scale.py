# Standard library imports
import re

"""Scale images by a percentage of their original size (IMAGE_SCALE_PERCENT in config.py)."""

_LENGTH = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(px)?\s*$", re.I)
_STYLE_PX = re.compile(r"(?<![\w-])(width|height)(\s*:\s*)(\d+(?:\.\d+)?)px", re.I)


def normalize_percent(percent):
	"""Return a usable percentage (float) or None when scaling is off (unset, 0, 100, or not a positive number)."""
	try:
		value = float(str(percent).strip().rstrip("%"))
	except (TypeError, ValueError):
		return None
	if value <= 0 or value == 100:
		return None
	return value


def scaled_size(width, height, percent):
	"""New (width, height) in whole pixels, never smaller than 1x1."""
	factor = percent / 100.0
	return max(1, int(round(width * factor))), max(1, int(round(height * factor)))


def _fmt(n):
	return str(max(1, int(round(n))))


def scale_length(value, percent):
	"""Scale an HTML width/height attribute like '800' or '800px'. Percentages, 'auto' and the like are returned unchanged."""
	m = _LENGTH.match(str(value))
	if not m:
		return value
	return _fmt(float(m.group(1)) * percent / 100.0) + (m.group(2) or "")


def scale_style_px(style, percent):
	"""Scale width/height values given in px inside an inline style attribute."""
	return _STYLE_PX.sub(lambda m: f"{m.group(1)}{m.group(2)}{_fmt(float(m.group(3)) * percent / 100.0)}px", style)


def scale_img_tags(soup, percent):
	"""Scale the displayed size of every <img> to match its scaled image file. Returns how many tags changed."""
	changed = 0
	for img in soup.find_all("img"):
		touched = False
		for attr in ("width", "height"):
			if img.has_attr(attr):
				new = scale_length(img[attr], percent)
				if new != img[attr]:
					img[attr] = new
					touched = True
		if img.get("style"):
			new = scale_style_px(img["style"], percent)
			if new != img["style"]:
				img["style"] = new
				touched = True
		changed += touched
	return changed
