# Standard library imports
import asyncio
import threading
import time
from urllib.parse import urlparse

"""
Run a page's JavaScript on the server, so browsers that cannot (or should not) run it get the finished page.

A headless Chromium (Playwright) loads the page, lets its scripts run until the network settles, and the
resulting DOM is serialized back to HTML. That HTML then goes through the normal pipeline (script removal,
CSS translation, layout emulation), so the client only ever receives static markup.

Settings (config.py, all optional):
  RENDER_JAVASCRIPT              True to render every HTML page this way (default False)
  RENDER_JAVASCRIPT_SKIP_DOMAINS domains (and subdomains) that are never rendered, e.g. ["example.com"]
  RENDER_TIMEOUT                 seconds a page may take before we use whatever has loaded (default 20)
  RENDER_MAX_CONCURRENT          pages rendered at the same time (default 2)
  RENDER_BLOCK_RESOURCES         resource types the renderer does not download: image, media, font, ...
                                 (default image, media, font; the DOM does not need them)
  RENDER_CACHE_SECONDS           how long a rendered page is reused (default 300)

Rendering is best-effort: on any failure the caller falls back to the page as the server sent it.
"""

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
CHROMIUM_ARGS = [
	"--no-sandbox",             # the container runs as root
	"--disable-dev-shm-usage",  # Docker's default /dev/shm is tiny
	"--disable-gpu",
	"--disable-background-networking",
	"--mute-audio",
]
SETTLE_AFTER_LOAD_MS = 400
CACHE_MAX_ENTRIES = 64

# Runs inside the page just before we read its HTML back.
SERIALIZE_JS = """() => {
	// CSS-in-JS libraries insert rules through the CSSOM, leaving <style> tags empty; copy the rules back into the text.
	for (const sheet of document.styleSheets) {
		try {
			const owner = sheet.ownerNode;
			if (owner && owner.tagName === 'STYLE' && !owner.textContent.trim() && sheet.cssRules.length) {
				owner.textContent = Array.from(sheet.cssRules).map(r => r.cssText).join('\\n');
			}
		} catch (e) {}
	}
	try {
		if (document.adoptedStyleSheets && document.adoptedStyleSheets.length) {
			const style = document.createElement('style');
			style.textContent = document.adoptedStyleSheets.map(s => Array.from(s.cssRules).map(r => r.cssText).join('\\n')).join('\\n');
			(document.head || document.documentElement).appendChild(style);
		}
	} catch (e) {}
	// The scripts have run, so the <noscript> fallbacks must not be shown as well.
	document.querySelectorAll('noscript').forEach(n => n.remove());
	const dt = document.doctype ? '<!DOCTYPE ' + document.doctype.name + '>' : '';
	return dt + document.documentElement.outerHTML;
}"""

# Scrolls through the page so lazy-loaded content (IntersectionObserver, infinite "load more" sentinels) gets built.
SCROLL_JS = """async () => {
	const step = Math.max(window.innerHeight, 400);
	const limit = Math.min(document.documentElement.scrollHeight, step * 12);
	for (let y = 0; y < limit; y += step) {
		window.scrollTo(0, y);
		await new Promise(r => setTimeout(r, 60));
	}
	window.scrollTo(0, 0);
}"""

_state = {"loop": None, "thread": None, "browser": None, "playwright": None, "sem": None, "disabled": False}
_state_lock = threading.Lock()
_cache = {}  # url -> (timestamp, html)


# ----------------------------------------------------------------------------------
# Decisions
# ----------------------------------------------------------------------------------

def _cfg(config, name, default):
	value = getattr(config, name, None)
	return default if value is None else value


def enabled(config):
	return bool(getattr(config, "RENDER_JAVASCRIPT", False)) and not _state["disabled"]


def is_skipped(url, config):
	host = urlparse(url).netloc.lower().split(":")[0]
	for domain in _cfg(config, "RENDER_JAVASCRIPT_SKIP_DOMAINS", []) or []:
		domain = domain.lower().lstrip(".")
		if host == domain or host.endswith("." + domain):
			return True
	return False


def should_render(url, config):
	return enabled(config) and not is_skipped(url, config)


# ----------------------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------------------

def cache_get(url, ttl):
	hit = _cache.get(url)
	if hit and time.time() - hit[0] < ttl:
		return hit[1]
	_cache.pop(url, None)
	return None


def cache_put(url, html):
	if len(_cache) >= CACHE_MAX_ENTRIES:
		_cache.pop(min(_cache, key=lambda k: _cache[k][0]))
	_cache[url] = (time.time(), html)


# ----------------------------------------------------------------------------------
# Browser (one long-lived Chromium, driven from a dedicated event-loop thread)
# ----------------------------------------------------------------------------------

def _ensure_loop():
	with _state_lock:
		if _state["loop"] is None:
			loop = asyncio.new_event_loop()

			def run():
				asyncio.set_event_loop(loop)
				loop.run_forever()

			thread = threading.Thread(target=run, name="render-loop", daemon=True)
			thread.start()
			_state["loop"], _state["thread"] = loop, thread
	return _state["loop"]


async def _get_browser(config):
	browser = _state["browser"]
	if browser is not None and browser.is_connected():
		return browser
	from playwright.async_api import async_playwright
	if _state["playwright"] is None:
		_state["playwright"] = await async_playwright().start()
	_state["browser"] = browser = await _state["playwright"].chromium.launch(args=CHROMIUM_ARGS)
	print("Render: headless Chromium started")
	return browser


async def _render(url, config, accept_language):
	from playwright.async_api import TimeoutError as PlaywrightTimeout

	timeout = float(_cfg(config, "RENDER_TIMEOUT", 20))
	blocked = set(_cfg(config, "RENDER_BLOCK_RESOURCES", ("image", "media", "font")))
	if _state["sem"] is None:
		_state["sem"] = asyncio.Semaphore(int(_cfg(config, "RENDER_MAX_CONCURRENT", 2)))

	async with _state["sem"]:
		started = time.time()
		browser = await _get_browser(config)
		context = await browser.new_context(
			user_agent=USER_AGENT,
			viewport={"width": int(_cfg(config, "CSS_VIEWPORT_WIDTH", 1024)), "height": int(_cfg(config, "CSS_VIEWPORT_HEIGHT", 768))},
			ignore_https_errors=True,
			locale=(accept_language or "en-US").split(",")[0].split(";")[0].strip() or "en-US",
		)
		try:
			page = await context.new_page()

			async def route(r):
				if r.request.resource_type in blocked:
					await r.abort()
				else:
					await r.continue_()

			await page.route("**/*", route)
			remaining = lambda: max(0.5, timeout - (time.time() - started))

			try:
				await page.goto(url, wait_until="domcontentloaded", timeout=remaining() * 1000)
			except PlaywrightTimeout:
				print(f"Render: {url} did not finish loading in {timeout:.0f}s; using what has loaded")
			for _ in range(2):
				try:
					await page.wait_for_load_state("networkidle", timeout=min(remaining(), 8) * 1000)
				except PlaywrightTimeout:
					break
				try:
					await page.evaluate(SCROLL_JS)
				except Exception:
					break
				await page.wait_for_timeout(SETTLE_AFTER_LOAD_MS)
			html = await page.evaluate(SERIALIZE_JS)
			print(f"Render: {url} -> {len(html) // 1024}KB in {time.time() - started:.1f}s")
			return html
		finally:
			await context.close()


def render_page(url, config, accept_language=None):
	"""Return the page's HTML after its JavaScript has run, or None if rendering failed (caller falls back)."""
	ttl = float(_cfg(config, "RENDER_CACHE_SECONDS", 300))
	cached = cache_get(url, ttl)
	if cached is not None:
		print(f"Render: {url} served from cache")
		return cached
	try:
		import playwright  # noqa: F401
	except ImportError:
		print("Render: the 'playwright' package is not installed; JavaScript rendering is disabled")
		_state["disabled"] = True
		return None
	timeout = float(_cfg(config, "RENDER_TIMEOUT", 20))
	loop = _ensure_loop()
	future = asyncio.run_coroutine_threadsafe(_render(url, config, accept_language), loop)
	try:
		# A little longer than the page budget: queueing behind other renders is not the page's fault
		html = future.result(timeout=timeout * 3 + 30)
	except Exception as e:
		future.cancel()
		print(f"Render: failed for {url}: {type(e).__name__}: {e}")
		if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
			print("Render: Chromium is not installed (run 'playwright install chromium'); JavaScript rendering is disabled")
			_state["disabled"] = True
		return None
	if html:
		cache_put(url, html)
	return html or None


def warm_up(config):
	"""Start Chromium in the background so the first page does not pay for the launch."""
	if not enabled(config):
		return

	def go():
		try:
			import playwright  # noqa: F401
		except ImportError:
			print("Render: RENDER_JAVASCRIPT is on but the 'playwright' package is not installed")
			_state["disabled"] = True
			return
		try:
			asyncio.run_coroutine_threadsafe(_get_browser(config), _ensure_loop()).result(timeout=60)
		except Exception as e:
			print(f"Render: could not start Chromium: {e}")

	threading.Thread(target=go, daemon=True).start()
