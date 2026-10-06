import io
import os
import sys
import types
import unittest

from bs4 import BeautifulSoup
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
	import PILSVG  # noqa: F401  (installed in the Docker image)
except ImportError:
	sys.modules["PILSVG"] = types.SimpleNamespace(SVG=None)  # SVG rendering is not exercised here
from utils import image_scale as S
from utils import image_utils as U


def png(w, h, color=(200, 30, 30)):
	buf = io.BytesIO()
	Image.new("RGB", (w, h), color).save(buf, "PNG")
	return buf.getvalue()


def size_of(data):
	return Image.open(io.BytesIO(data)).size


class PercentTests(unittest.TestCase):
	def test_off_values(self):
		for v in (None, 0, 100, "100", "100%", -5, "abc", "", False):
			self.assertIsNone(S.normalize_percent(v), v)

	def test_on_values(self):
		self.assertEqual(S.normalize_percent(25), 25.0)
		self.assertEqual(S.normalize_percent("25%"), 25.0)
		self.assertEqual(S.normalize_percent(" 12.5 "), 12.5)
		self.assertEqual(S.normalize_percent(200), 200.0)

	def test_scaled_size(self):
		self.assertEqual(S.scaled_size(2000, 1500, 25), (500, 375))
		self.assertEqual(S.scaled_size(3, 3, 25), (1, 1))  # never zero
		self.assertEqual(S.scaled_size(101, 99, 50), (50, 50))


class AttributeTests(unittest.TestCase):
	def test_scale_length(self):
		self.assertEqual(S.scale_length("800", 25), "200")
		self.assertEqual(S.scale_length("800px", 25), "200px")
		self.assertEqual(S.scale_length(" 40 ", 25), "10")
		self.assertEqual(S.scale_length("2", 25), "1")
		for untouched in ("100%", "auto", "", "5em"):
			self.assertEqual(S.scale_length(untouched, 25), untouched)

	def test_style(self):
		self.assertEqual(S.scale_style_px("width:400px;height:200px;color:red", 50), "width:200px;height:100px;color:red")
		self.assertEqual(S.scale_style_px("max-width:400px;line-height:20px", 50), "max-width:400px;line-height:20px")
		self.assertEqual(S.scale_style_px("width: 100%", 50), "width: 100%")

	def test_img_tags(self):
		soup = BeautifulSoup('<img src="a" width="800" height="600"><img src="b" width="50%"><img src="c" style="width:300px"><img src="d"><p width="9">', "html5lib")
		self.assertEqual(S.scale_img_tags(soup, 25), 2)  # the 50%-wide and attribute-less images need no change
		a, b, c, d = soup.find_all("img")
		self.assertEqual((a["width"], a["height"]), ("200", "150"))
		self.assertEqual(b["width"], "50%")
		self.assertEqual(c["style"], "width:75px")
		self.assertFalse(d.has_attr("width"))
		self.assertEqual(soup.p["width"], "9")


class OptimizeImageTests(unittest.TestCase):
	def run_opt(self, w, h, **kw):
		defaults = dict(resize=False, convert=False, convert_to=None, dithering=None)
		defaults.update(kw)
		return size_of(U.optimize_image(png(w, h), **defaults))

	def test_scale_only(self):
		self.assertEqual(self.run_opt(2000, 1500, scale_percent=25), (500, 375))
		self.assertEqual(self.run_opt(2000, 1500, scale_percent="50%"), (1000, 750))

	def test_off_leaves_size_alone(self):
		for v in (None, 100, 0):
			self.assertEqual(self.run_opt(640, 480, scale_percent=v), (640, 480))

	def test_cap_still_applies_after_scaling(self):
		# 50% of 3000x2000 is 1500x1000, which the 1024x768 cap then reduces further
		self.assertEqual(self.run_opt(3000, 2000, scale_percent=50, resize=True, max_width=1024, max_height=768), (1024, 682))

	def test_cap_not_triggered_when_scaled_image_fits(self):
		self.assertEqual(self.run_opt(3000, 2000, scale_percent=25, resize=True, max_width=1024, max_height=768), (750, 500))

	def test_tiny_images_stay_valid(self):
		self.assertEqual(self.run_opt(3, 2, scale_percent=25), (1, 1))

	def test_with_conversion(self):
		out = U.optimize_image(png(400, 400), resize=False, convert=True, convert_to="png", dithering=None, scale_percent=25)
		self.assertEqual(size_of(out), (100, 100))
		self.assertEqual(Image.open(io.BytesIO(out)).format, "PNG")

	def test_original_format_is_kept_when_not_converting(self):
		# Regression: a resized image has no .format, which used to make save() fail and silently return the original
		buf = io.BytesIO()
		Image.new("RGB", (800, 600), (10, 200, 10)).save(buf, "JPEG")
		out = U.optimize_image(buf.getvalue(), resize=True, max_width=400, max_height=400, convert=False, convert_to=None, dithering=None)
		img = Image.open(io.BytesIO(out))
		self.assertEqual((img.format, img.size), ("JPEG", (400, 300)))

	def test_cache_file_gets_extension_of_real_format(self):
		import hashlib
		buf = io.BytesIO()
		Image.new("RGB", (100, 100), (1, 2, 3)).save(buf, "JPEG")
		url = "http://test.invalid/photo-" + hashlib.md5(os.urandom(8)).hexdigest()
		cached = U.fetch_and_cache_image(url, buf.getvalue(), resize=False, convert=False, convert_to=None, dithering=None, scale_percent=50)
		path = os.path.join(U.CACHE_DIR, os.path.basename(cached))
		try:
			self.assertTrue(cached.endswith(".jpg"), cached)
			self.assertEqual(Image.open(path).size, (50, 50))
			# second request is served from the cache under the same name
			self.assertEqual(U.fetch_and_cache_image(url, None, resize=False, convert=False, convert_to=None, dithering=None, scale_percent=50), cached)
		finally:
			os.unlink(path)

	def test_converted_file_name_is_deterministic(self):
		import hashlib
		url = "http://test.invalid/c-" + hashlib.md5(os.urandom(8)).hexdigest()
		cached = U.fetch_and_cache_image(url, png(64, 64), resize=False, convert=True, convert_to="jpeg", dithering=None)
		try:
			self.assertTrue(cached.endswith(".jpg"), cached)
		finally:
			os.unlink(os.path.join(U.CACHE_DIR, os.path.basename(cached)))

	def test_scaling_alone_triggers_processing(self):
		# With resize and convert both off, a scale percentage must still be applied by fetch_and_cache_image
		import hashlib
		url = "http://test.invalid/pic-" + hashlib.md5(os.urandom(8)).hexdigest()
		cached = U.fetch_and_cache_image(url, png(800, 800), resize=False, convert=False, convert_to=None, dithering=None, scale_percent=25)
		path = os.path.join(U.CACHE_DIR, os.path.basename(cached))
		try:
			self.assertEqual(Image.open(path).size, (200, 200))
		finally:
			os.unlink(path)


if __name__ == "__main__":
	unittest.main()
