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
  RENDER_FORMS                   True (default) to route forms that a page's scripts handle (no real action) back
                                 through the renderer, so JavaScript-driven login forms work

Cookies: the renderer shares the client's cookie jar (utils/cookie_utils.py) with the proxy. Cookies are loaded
into the headless browser before a page loads and read back afterwards, so a login made by either one is seen
by the other. Rendered pages are cached per client, never shared between clients.

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
SERIALIZE_JS = """(opts) => {
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
	// Forms that the page's own scripts handle (no real action) would do nothing, or submit to the wrong place, in a
	// browser without those scripts. Send them to the proxy, which replays the submission in the headless browser.
	if (opts && opts.forms) {
		const pageUrl = location.href;
		Array.from(document.forms).forEach((f, i) => {
			const action = (f.getAttribute('action') || '').trim();
			if (!(action === '' || action === '#' || /^javascript:/i.test(action))) return;
			const method = (f.getAttribute('method') || 'get').toLowerCase();
			if (method === 'dialog') return;
			f.setAttribute('action', '/__mp/form');
			f.setAttribute('method', 'post');
			f.removeAttribute('enctype');
			f.removeAttribute('onsubmit');
			[['__mp_url', pageUrl], ['__mp_form', String(i)], ['__mp_method', method]].forEach(([n, v]) => {
				const h = document.createElement('input');
				h.type = 'hidden'; h.name = n; h.value = v;
				f.appendChild(h);
			});
		});
	}
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
# Cache (per client: a logged-in page must never be served to someone else)
# ----------------------------------------------------------------------------------

def norm_url(url):
	"""A URL without scheme or fragment, so http:// and https:// forms of the same page match."""
	parsed = urlparse(url)
	return f"{parsed.netloc.lower()}{parsed.path or '/'}" + (f"?{parsed.query}" if parsed.query else "")


def _key(client, url):
	return (client, norm_url(url))


def cache_get(url, ttl, client=None):
	hit = _cache.get(_key(client, url))
	if hit and time.time() - hit[0] < ttl:
		return hit[1]
	_cache.pop(_key(client, url), None)
	return None


def cache_put(url, html, client=None):
	if len(_cache) >= CACHE_MAX_ENTRIES:
		_cache.pop(min(_cache, key=lambda k: _cache[k][0]))
	_cache[_key(client, url)] = (time.time(), html)


HANDOFF_SECONDS = 120
_handoff = {}  # (client, url) -> (timestamp, html): the page produced by a form submission, shown once on the redirect


def handoff_put(client, url, html):
	for k in [k for k, v in _handoff.items() if time.time() - v[0] > HANDOFF_SECONDS]:
		_handoff.pop(k, None)
	_handoff[_key(client, url)] = (time.time(), html)


def handoff_pop(client, url):
	hit = _handoff.pop(_key(client, url), None)
	if hit and time.time() - hit[0] < HANDOFF_SECONDS:
		return hit[1]
	return None


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


async def _new_context(browser, config, accept_language, cookies, auth):
	options = dict(
		user_agent=USER_AGENT,
		viewport={"width": int(_cfg(config, "CSS_VIEWPORT_WIDTH", 1024)), "height": int(_cfg(config, "CSS_VIEWPORT_HEIGHT", 768))},
		ignore_https_errors=True,
		locale=(accept_language or "en-US").split(",")[0].split(";")[0].strip() or "en-US",
	)
	if auth:
		options["http_credentials"] = {"username": auth[0], "password": auth[1]}
	context = await browser.new_context(**options)
	if cookies:
		try:
			await context.add_cookies(cookies)
		except Exception:
			# One malformed cookie must not lose the whole session: add them one at a time
			for cookie in cookies:
				try:
					await context.add_cookies([cookie])
				except Exception:
					pass
	return context


# Playwright resource types as the filter lists name them
AD_RESOURCE_TYPES = {"document": "subdocument", "stylesheet": "stylesheet", "image": "image", "media": "media", "font": "font",
					 "script": "script", "xhr": "xmlhttprequest", "fetch": "xmlhttprequest", "ping": "ping", "websocket": "websocket"}


async def _prepare_page(context, config, target_url=""):
	"""A page that does not download what the DOM does not need, nor anything the ad blocker refuses."""
	from utils import adblock

	blocked = set(_cfg(config, "RENDER_BLOCK_RESOURCES", ("image", "media", "font")))
	ads = adblock.get()
	first_party = (urlparse(target_url).hostname or "").lower()
	page = await context.new_page()

	async def route(r):
		request = r.request
		if request.resource_type in blocked:
			return await r.abort()
		if ads is not None and ads.enabled:
			# The page being rendered is never blocked, only what it loads
			is_main_navigation = request.is_navigation_request() and request.frame == page.main_frame
			if not is_main_navigation:
				rtype = AD_RESOURCE_TYPES.get(request.resource_type, "other")
				if ads.should_block(request.url, rtype, first_party):
					return await r.abort()
		await r.continue_()

	await page.route("**/*", route)
	return page


async def _settle(page, started, timeout, scroll=True):
	"""Wait for the page's scripts to finish (network idle), scrolling once so lazy content gets built."""
	from playwright.async_api import TimeoutError as PlaywrightTimeout

	remaining = lambda: max(0.5, timeout - (time.time() - started))
	for _ in range(2):
		try:
			await page.wait_for_load_state("networkidle", timeout=min(remaining(), 8) * 1000)
		except PlaywrightTimeout:
			break
		if scroll:
			try:
				await page.evaluate(SCROLL_JS)
			except Exception:
				break
		await page.wait_for_timeout(SETTLE_AFTER_LOAD_MS)


def _forms_enabled(config):
	return bool(_cfg(config, "RENDER_FORMS", True))


async def _render(url, config, accept_language, cookies, auth):
	from playwright.async_api import TimeoutError as PlaywrightTimeout

	timeout = float(_cfg(config, "RENDER_TIMEOUT", 20))
	if _state["sem"] is None:
		_state["sem"] = asyncio.Semaphore(int(_cfg(config, "RENDER_MAX_CONCURRENT", 2)))

	async with _state["sem"]:
		started = time.time()
		browser = await _get_browser(config)
		context = await _new_context(browser, config, accept_language, cookies, auth)
		try:
			page = await _prepare_page(context, config, url)
			try:
				await page.goto(url, wait_until="domcontentloaded", timeout=max(0.5, timeout - (time.time() - started)) * 1000)
			except PlaywrightTimeout:
				print(f"Render: {url} did not finish loading in {timeout:.0f}s; using what has loaded")
			await _settle(page, started, timeout)
			html = await page.evaluate(SERIALIZE_JS, {"forms": _forms_enabled(config)})
			jar_after = await context.cookies()
			print(f"Render: {url} -> {len(html) // 1024}KB in {time.time() - started:.1f}s")
			return html, jar_after
		finally:
			await context.close()


# Fills the posted values into the form on the freshly loaded page and submits it the way a person would.
SUBMIT_JS = """(a) => {
	const posted = a.fields.filter(f => !f[0].startsWith('__mp_'));
	const names = new Set(posted.map(f => f[0]));
	const overlap = form => {
		const have = new Set(Array.from(form.elements).map(e => e.name).filter(Boolean));
		let n = 0; names.forEach(x => { if (have.has(x)) n++; });
		return n;
	};
	let form = document.forms[a.index];
	let best = form ? overlap(form) : -1;
	Array.from(document.forms).forEach(f => { const o = overlap(f); if (o > best) { best = o; form = f; } });
	if (!form) return {ok: false, reason: 'no form on the page'};

	const values = {};
	posted.forEach(([n, v]) => { (values[n] = values[n] || []).push(v); });
	const setNative = (el, v) => {
		const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
		Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);   // works with React-style controlled inputs
		el.dispatchEvent(new Event('input', {bubbles: true}));
		el.dispatchEvent(new Event('change', {bubbles: true}));
	};
	Array.from(form.elements).forEach(el => {
		if (!el.name || el.disabled) return;
		const type = (el.type || '').toLowerCase();
		const mine = values[el.name] || [];
		if (type === 'checkbox') {
			if (el.checked !== mine.includes(el.value)) el.click();
		} else if (type === 'radio') {
			if (mine.includes(el.value) && !el.checked) el.click();
		} else if (el.tagName === 'SELECT') {
			if (el.name in values) {
				Array.from(el.options).forEach(o => { o.selected = mine.includes(o.value); });
				el.dispatchEvent(new Event('input', {bubbles: true}));
				el.dispatchEvent(new Event('change', {bubbles: true}));
			}
		} else if (['hidden', 'submit', 'button', 'image', 'reset', 'file'].includes(type)) {
			// not filled in by a person
		} else if (el.name in values) {
			setNative(el, mine[0]);
		}
	});

	// The button the person pressed is the one whose name=value came back in the posted fields
	let button = Array.from(form.querySelectorAll('button, input[type=submit], input[type=image]'))
		.find(b => b.name && a.fields.some(p => p[0] === b.name && p[1] === b.value)) || null;
	if (!button) button = form.querySelector('button[type=submit], input[type=submit], button:not([type])');
	// Submit on the next tick so this call returns before a navigation tears the page down
	setTimeout(() => {
		if (button) button.click();
		else if (form.requestSubmit) form.requestSubmit();
		else form.submit();
	}, 0);
	return {ok: true, reason: ''};
}"""


async def _submit(url, index, fields, config, accept_language, cookies, auth):
	from playwright.async_api import TimeoutError as PlaywrightTimeout

	timeout = float(_cfg(config, "RENDER_TIMEOUT", 20))
	if _state["sem"] is None:
		_state["sem"] = asyncio.Semaphore(int(_cfg(config, "RENDER_MAX_CONCURRENT", 2)))

	async with _state["sem"]:
		started = time.time()
		browser = await _get_browser(config)
		context = await _new_context(browser, config, accept_language, cookies, auth)
		try:
			page = await _prepare_page(context, config, url)
			try:
				await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
			except PlaywrightTimeout:
				print(f"Render: {url} did not finish loading in {timeout:.0f}s; submitting anyway")
			await _settle(page, started, timeout, scroll=False)
			outcome = await page.evaluate(SUBMIT_JS, {"index": index, "fields": fields})
			if not outcome.get("ok"):
				print(f"Render: form submission to {url} not possible: {outcome.get('reason')}")
				return None
			# The submission may navigate or just update the page; either way wait for things to settle
			await page.wait_for_timeout(500)
			await _settle(page, started, timeout)
			html = await page.evaluate(SERIALIZE_JS, {"forms": _forms_enabled(config)})
			final_url = page.url
			jar_after = await context.cookies()
			print(f"Render: submitted form on {url} -> {final_url} in {time.time() - started:.1f}s")
			return html, final_url, jar_after
		finally:
			await context.close()


def _run(coro, config):
	timeout = float(_cfg(config, "RENDER_TIMEOUT", 20))
	future = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
	try:
		# A little longer than the page budget: queueing behind other renders is not the page's fault
		return future.result(timeout=timeout * 3 + 30)
	except Exception as e:
		future.cancel()
		print(f"Render: failed: {type(e).__name__}: {e}")
		if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
			print("Render: Chromium is not installed (run 'playwright install chromium'); JavaScript rendering is disabled")
			_state["disabled"] = True
		return None


def _have_playwright():
	try:
		import playwright  # noqa: F401
		return True
	except ImportError:
		print("Render: the 'playwright' package is not installed; JavaScript rendering is disabled")
		_state["disabled"] = True
		return False


def render_page(url, config, accept_language=None, client=None, store=None, auth=None):
	"""Return the page's HTML after its JavaScript has run, or None if rendering failed (caller falls back).

	`store` (a CookieStore) and `client` select the cookie jar to use and update; `auth` is an optional
	(username, password) pair for HTTP Basic authentication."""
	ttl = float(_cfg(config, "RENDER_CACHE_SECONDS", 300))
	cached = cache_get(url, ttl, client)
	if cached is not None:
		print(f"Render: {url} served from cache")
		return cached
	if not _have_playwright():
		return None
	cookies = store.for_playwright(client) if store is not None else []
	result = _run(_render(url, config, accept_language, cookies, auth), config)
	if not result:
		return None
	html, cookies_after = result
	if store is not None:
		store.merge_playwright(client, cookies_after)
	if html:
		cache_put(url, html, client)
	return html or None


def submit_form(url, index, fields, config, accept_language=None, client=None, store=None, auth=None):
	"""Load `url` in the headless browser, fill in `fields` ([(name, value), ...]) on form number `index`,
	submit it, and return (final_url, html) of what the page looks like afterwards, or None on failure."""
	if not _have_playwright():
		return None
	cookies = store.for_playwright(client) if store is not None else []
	result = _run(_submit(url, index, fields, config, accept_language, cookies, auth), config)
	if not result:
		return None
	html, final_url, cookies_after = result
	if store is not None:
		store.merge_playwright(client, cookies_after)
	return final_url, html


def warm_up(config):
	"""Start Chromium in the background so the first page does not pay for the launch."""
	if not enabled(config):
		return

	def go():
		if not _have_playwright():
			return
		try:
			asyncio.run_coroutine_threadsafe(_get_browser(config), _ensure_loop()).result(timeout=60)
		except Exception as e:
			print(f"Render: could not start Chromium: {e}")

	threading.Thread(target=go, daemon=True).start()
