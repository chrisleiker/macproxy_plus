import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
	import flask  # noqa: F401
	import PILSVG  # noqa: F401
	HAVE_STACK = True
except ImportError:
	HAVE_STACK = False


@unittest.skipUnless(HAVE_STACK, "needs Flask and pillow-svg (available in the Docker image)")
class UseTagTests(unittest.TestCase):
	"""Regression: <use> elements that do not point at a <symbol> used to crash the whole page."""

	@classmethod
	def setUpClass(cls):
		# html_utils reads its settings from a `config` module, which the bare image does not have
		if "config" not in sys.modules:
			sys.modules["config"] = types.SimpleNamespace(
				PRESET=None, CONVERT_IMAGES=False, CONVERT_IMAGES_TO_FILETYPE=None, RESIZE_IMAGES=False,
				MAX_IMAGE_WIDTH=None, MAX_IMAGE_HEIGHT=None, DITHERING_ALGORITHM=None)

	def transcode(self, html):
		from flask import Flask
		from utils import html_utils
		app = Flask(__name__)
		app.config["MACPROXY_HOST_AND_PORT"] = "proxy.test:5001"

		@app.route("/cached_image/<path:filename>")
		def serve_cached_image(filename):
			return ""

		with app.test_request_context():
			return html_utils.transcode_html(html, "http://example.test/", whitelisted_domains=[], simplify_html=False,
											 tags_to_unwrap=[], tags_to_strip=[], attributes_to_strip=[],
											 convert_characters=False, conversion_table={}).decode()

	def test_use_pointing_at_a_shape_does_not_crash(self):
		out = self.transcode('<p>hi</p><svg viewBox="0 0 20 20" width="20" height="20"><path id="q" d="M1 1h5v5z"></path><use href="#q" x="2"></use></svg>')
		self.assertIn("<p>hi</p>", out)

	def test_use_pointing_at_an_external_sprite_does_not_crash(self):
		out = self.transcode('<p>hi</p><svg width="10" height="10"><use xlink:href="/sprite.svg#icon"></use></svg>')
		self.assertIn("<p>hi</p>", out)

	def test_use_with_no_href_does_not_crash(self):
		self.assertIn("<p>hi</p>", self.transcode('<p>hi</p><svg width="10" height="10"><use></use></svg>'))

	def test_use_pointing_at_a_symbol_is_still_inlined(self):
		out = self.transcode('<svg style="display:none"><symbol id="s" viewBox="0 0 8 8"><rect width="8" height="8"/></symbol></svg>'
							 '<svg width="8" height="8"><use href="#s"></use></svg><p>x</p>')
		self.assertIn("<p>x</p>", out)


if __name__ == "__main__":
	unittest.main()
