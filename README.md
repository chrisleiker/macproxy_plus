## MacProxy Plus
An extensible HTTP proxy that connects early computers to the Internet.

This fork of <a href="https://github.com/rdmark/macproxy">MacProxy</a> adds support for ```extensions```, which intercept requests for specific domains to serve simplified HTML interfaces, making it possible to browse the modern web from vintage hardware. Though originally designed for compatibility with early Macintoshes, MacProxy Plus should work to get many other vintage machines online.

### Demonstration Video (on YouTube)

<a href="https://youtu.be/f1v1gWLHcOk" target="_blank">
  <img src="./readme_images/youtube_thumbnail.jpg" alt="Teaching an Old Mac New Tricks" width="400">
</a>

### Extensions

Each extension has its own folder within the `extensions` directory. Extensions can be individually enabled or disabled via a `config.py` file in the root directory.

To enable extensions:

1. In the root directory, rename ```config.py.example``` to ```config.py``` :

	```shell
	mv config.py.example config.py
	```

2. In ```config.py```, enable/disable extensions by uncommenting/commenting lines in the ```ENABLED_EXTENSIONS``` list:

	```python
	ENABLED_EXTENSIONS = [
		#disabled_extension,
		"enabled_extension"
		]
	```

### CSS translation for older browsers

By default, macproxy strips all styling from pages. A preset can instead set `CSS_MODE = "downlevel"`, which keeps each site's CSS but translates it for the target browser (see `utils/css_utils.py`):

- `@import` and CSS nesting are flattened, `@layer` is unwrapped, `@media` / `@supports` are evaluated against a fixed viewport (`CSS_VIEWPORT_WIDTH` x `CSS_VIEWPORT_HEIGHT`)
- `var()`, `calc()`, `min()`/`max()`/`clamp()`, `rem` and viewport units are resolved to plain values
- `rgba()`/`hsl()`/`#rrggbbaa` become opaque hex colors (blended on white); gradients fall back to their first color
- unsupported properties, values (e.g. `display: flex`), selectors and at-rules are dropped, so the browser never sees syntax it can't parse

Two presets use it, selected with `PRESET = "..."` in `config.py`:

- `classilla` - Classilla on Mac OS 9 (Mozilla 1.3.1 engine): full translation
- `powerfox` - PowerFox on Mac OS X 10.4 Tiger (modern UXP engine): only newer syntax is translated

#### Flexbox and grid emulation

Browsers with no flexbox/grid support (Classilla) would otherwise stack every flex/grid item vertically. With `LAYOUT_EMULATION = True` (enabled in the `classilla` preset), `utils/layout_utils.py` reads the page's stylesheets on the server, works out which elements are flex/grid containers, and re-expresses their layout with CSS 2.1 table display values and floats, as inline styles:

| Modern layout | Emulated as |
|---|---|
| `flex` row | `display:table` container, `display:table-cell` items (`gap` -> `border-spacing`, `flex-grow` / `flex-basis` -> cell widths, `justify-content` / `align-items` -> margins / `vertical-align`) |
| `flex-wrap: wrap` | floated items plus a clearing element |
| `flex-direction: column` | normal block flow (gap -> margins, centered items shrink-wrapped) |
| `grid` with N columns | `display:table` container, rows wrapped in `display:table-row`, cells sized to the tracks (`repeat()`, `fr`, `px`, `%`, `auto-fill` / `auto-fit`) |

Spacing is compensated so edges and total height match the modern layout. Layouts that cannot be expressed (reversed directions, `order`, grid spans / named areas / explicit placement) fall back to plain stacking. Linked stylesheets are fetched and cached for 10 minutes, and emulation is skipped (the page is served unmodified) if it takes longer than a few seconds.

The `CSS_UNSUPPORTED_*` lists in `presets/classilla/classilla.py` and `presets/powerfox/powerfox.py` are educated guesses; tune them against the real browser. Scripts are still stripped, and CSS translation is skipped for `WHITELISTED_DOMAINS`. To run the unit tests: `python -m unittest tests.test_css_utils tests.test_layout_utils tests.test_site_overrides tests.test_image_scale tests.test_render_utils tests.test_html_utils_svg tests.test_cookie_utils tests.test_login_cookies tests.test_adblock tests.test_adblock_proxy` (needs `tinycss2`, `beautifulsoup4`, `html5lib`, `Pillow`; the rendering and SVG tests are skipped unless Playwright / Flask / pillow-svg are installed, so the easiest way to run everything is inside the Docker image: `docker run --rm --entrypoint python3 macproxy_plus -m unittest discover -s tests -t .`).

### Scaling images by a percentage

Set `IMAGE_SCALE_PERCENT` in `config.py` to shrink every image to a percentage of its original size, for example `IMAGE_SCALE_PERCENT = 25` for a quarter (leave it `None`, or `100`, to keep the original size):

```python
IMAGE_SCALE_PERCENT = 25
```

- It is applied before the `MAX_IMAGE_WIDTH` / `MAX_IMAGE_HEIGHT` cap, which still limits the result when `RESIZE_IMAGES` is `True`. Set `RESIZE_IMAGES = False` if you want the percentage alone.
- `<img>` `width` / `height` attributes and inline `width:NNpx` / `height:NNpx` styles are scaled by the same percentage, so the page lays out at the smaller size instead of stretching the small image back up. Percentage and `auto` sizes are left alone. Images sized by an external stylesheet keep the stylesheet's size.
- It works with every preset (presets do not set it), and with inline SVGs. Tiny images shrink too (a 24px icon at 25% is 6px), never below 1px.

### Running JavaScript on the server

Many modern sites (for example instapaper.com) are built entirely by JavaScript in the browser, so with scripts stripped they load blank. Set `RENDER_JAVASCRIPT = True` in `config.py` and macproxy loads each HTML page in a headless Chromium on the server, lets its scripts run, and sends your browser the finished page (still passed through script removal, CSS translation and layout emulation), so the browser never has to run any JavaScript itself:

```python
RENDER_JAVASCRIPT = True
RENDER_JAVASCRIPT_SKIP_DOMAINS = ["example.com"]   # optional: never render these
```

- It is off by default. The Docker image includes Chromium (which makes the image roughly 2GB); if you run macproxy without Docker, install it with `pip install -r requirements.txt && playwright install chromium`.
- Each page is rendered at the viewport size from your preset (`CSS_VIEWPORT_WIDTH` x `CSS_VIEWPORT_HEIGHT`), scrolled through once to trigger lazy-loaded content, and cached for `RENDER_CACHE_SECONDS` (default 300). A page that never stops loading is used as it stands after `RENDER_TIMEOUT` seconds (default 20).
- Images, media and fonts are not downloaded while rendering (`RENDER_BLOCK_RESOURCES`); your browser fetches them through the proxy as usual.
- It is best-effort: if rendering fails, the page is served exactly as the site sent it. Only `GET` pages are rendered; form posts and other requests are untouched.
- Cookies and logins are supported (see below). Buttons that only work through JavaScript, other than form submit buttons, still do nothing in the result; ordinary links and forms work.
- Rendering takes a few seconds the first time and a few hundred MB of memory; `RENDER_MAX_CONCURRENT` (default 2) limits how many pages render at once. For Docker, `docker-compose.yml` sets `shm_size: "1gb"`; add the same to your TrueNAS app.
- `theverge.com`'s "no JavaScript" rule (below) still means no script is *sent to your browser*; with rendering on, the site's scripts do run on the server first.

### Ad and tracker blocking

Set `ADBLOCK = True` in `config.py` to block ads and trackers, which also saves an old machine from downloading them:

```python
ADBLOCK = True
```

It works in three places: (1) requests your browser makes to ad and tracker URLs are answered with an empty stand-in (a 1x1 GIF, empty script or stylesheet) instead of being fetched; (2) the headless browser used for `RENDER_JAVASCRIPT` is not allowed to load them either, which makes rendering faster and keeps tracking cookies out; (3) elements still left in a page are removed: tags that load a blocked URL, plus whatever the filter lists' element-hiding rules match (ad slots, "sponsored" boxes, ad-feedback pop-ups).

- **Filter lists** are downloaded in the background and refreshed daily: EasyList, EasyPrivacy and Peter Lowe's list by default (about 110,000 blocking rules and 30,000 element rules). Until they arrive, and whenever there is no network, a small built-in list is used. Downloaded lists are cached in `ADBLOCK_CACHE_DIR` (default `/app/data/adblock` if that folder exists, so a mounted `./data` volume keeps them across restarts). Use `ADBLOCK_LISTS = [...]` to choose your own lists (URLs or file paths; Adblock Plus/uBlock syntax or hosts files).
- **Fixing a broken site:** `ADBLOCK_ALLOWLIST = ["example.com"]` turns blocking off for a domain and for every page on it. `http://<any-site>/__mp/adblock` shows the lists, what has been blocked, and a box to test a URL ("would this be blocked, and by which rule?").
- **Your own rules** go in `ADBLOCK_CUSTOM_RULES = ["||ads.example.com^", "example.com##.promo"]`. `ADBLOCK_COSMETIC = False` blocks requests but leaves page elements alone.
- A page you type or bookmark yourself is never blocked, and neither are pages served by extensions. Rules that need features macproxy lacks (scriptlets, `:has-text()`, redirects, ...) are skipped, not guessed at.
- It costs about 60MB of memory and well under half a second per page.

### Logins and cookies

> **Switched off for now.** Cookie and login support is disabled by default. Add `COOKIE_SUPPORT = True` to `config.py` to turn it back on; everything below describes how it behaves when it is on. While it is off, the proxy remembers no cookies between requests (cookies still work inside a single request, such as a redirect chain), the headless browser starts each page with none, script-handled forms are left as the page has them, and the `/__mp/cookies` and `/__mp/form` addresses return 404. `Set-Cookie` is never passed on to your browser either way.

Your browser never stores or sends a site's cookies. Instead macproxy keeps a **cookie jar on the server for each device** (identified by its IP address), used both for the proxy's own requests and by the headless browser, so a login made one way is seen by the other.

- **Classic login forms** (a normal `<form method="post">`, like instapaper.com/user/login) just work: the form is posted to the site over `https://` first (so the password never goes upstream unencrypted), with a matching `Origin`, and the cookies it sets stay on the server. A login that redirects to a JavaScript-built page is rendered with those cookies.
- **JavaScript-driven login forms** (no real `action`, handled by the page's scripts) need `RENDER_JAVASCRIPT = True`. The proxy points such a form back at itself; on submit it reloads the page in the headless browser, fills in the values you typed, presses the same button, waits for the page to react, and shows you the result (including error messages). Only forms on the site you are viewing can be replayed.
- **HTTP Basic authentication** prompts work: the credentials your browser sends are forwarded (over `https://` when the site has it) and given to the renderer.
- `http://<any-site>/__mp/cookies` shows which sites have cookies stored for your device (names only, never values) and lets you forget one site or everything.
- Cookies are kept in memory by default, so a restart logs you out. To keep them, set `COOKIE_JAR_FILE` (the file is written with permissions 0600; mount a volume for it in Docker). `COOKIE_CLIENT_KEY = "global"` makes every device share one jar. `COOKIE_MAX_CLIENTS` and `COOKIE_MAX_PER_CLIENT` bound the memory used.

```python
COOKIE_JAR_FILE = "/app/data/cookies.json"   # optional: keep logins across restarts
```

Things to know: the leg between your browser and macproxy is plain `http://`, so a password you type crosses your network unencrypted. Use this only on a network you trust, and do not expose the proxy to the internet. Anyone who can reach the proxy from the same IP address shares that device's logins. Cookies are stored in plain text on the server (and in the `COOKIE_JAR_FILE`, if set). The form replay does not support file uploads or CAPTCHAs, and two-factor prompts that need a second page work only if they are ordinary forms.

### Per-site special cases

`utils/site_overrides.py` holds settings that apply to a specific site (the domain and its subdomains) whichever preset is active. The only setting so far is `strip_javascript`, which removes every `<script>`, inline `on*` handler, `javascript:` link and script preload from the site's pages, and serves its script files empty. `theverge.com` has it on by default. Add your own, or switch a default off, in `config.py`:

```python
SITE_OVERRIDES = {
	"example.com": {"strip_javascript": True},
	"theverge.com": {"strip_javascript": False},
}
```

### Running with Docker

```shell
cp config.py.example config.py   # edit to enable extensions / add API keys
docker compose up -d --build
```

The proxy listens on port `5001` (change with `PORT=8080 docker compose up -d`). `config.py` is mounted read-only into the container; edit it and run `docker compose restart` to apply changes. Requirements for all extensions are baked into the image.

### Starting MacProxy Plus

On Unix-like systems (such as Linux or macOS), run the ```start_macproxy.sh``` script. It will create a Python virtual environment, install the required Python packages, and make the proxy server available on your local network.

```shell
./start_macproxy.sh
```

On Windows, run the analogous PowerShell script, ```start_macproxy.ps1```:

```powershell
.\start_macproxy.ps1
```

### Connecting to MacProxy Plus from your Vintage Machine
To use MacProxy Plus, you'll need to configure your vintage browser or operating system to connect to the proxy server running on your host machine. The specific steps will vary depending on your browser and OS, but if your system lets you set a proxy server, it should work.

If you're using a BlueSCSI to get a vintage Mac online, <a href="https://bluescsi.com/docs/WiFi-DaynaPORT">this guide</a> should help with the initial Internet setup.
<br><br>
![Configuring proxy settings in MacWeb 2.0c+](readme_images/proxy_settings.gif)
<br>*Example: Configuring proxy settings in <a href="https://github.com/hunterirving/macweb-2.0c-plus">MacWeb 2.0c+</a>*

### Example Extension: ChatGPT

A ChatGPT extension is provided as an example. This extension serves a simple web interface that lets users interact with OpenAI's GPT models.

To enable the ChatGPT extension, open ```config.py```, uncomment the ```chatgpt``` line in the ```ENABLED_EXTENSIONS``` list, and replace ```YOUR_OPENAI_API_KEY_HERE``` with your actual OpenAI API key.

```python
open_ai_api_key = "YOUR_OPENAI_API_KEY_HERE"

ENABLED_EXTENSIONS = [
	"chatgpt"
]
```

Once enabled, Macproxy will reroute requests to ```http://chatgpt.com``` to this inteface.
<br><br>
<img src="readme_images/macintosh_plus.jpg">

### Other Extensions

#### Claude (Anthropic)
For the discerning LLM connoisseur.

#### Weather
Get the forecast for any zip code in the US.

#### Wikipedia
Read any of over 6 million encyclopedia articles - complete with clickable links and search function.

#### Reddit
Browse any subreddit or the Reddit homepage, with support for nested comments and downloadable images... in dithered black and white.

#### WayBack Machine
Enter any date between January 1st, 1996 and today, then browse the web as it existed at that point in time. Includes full download support for images and other files backed up by the Internet Archive.

#### Web Simulator
Type a URL that doesn't exist into the address bar, and Anthropic's Claude 3.5 Sonnet will interpret the domain and any query parameters to generate an imagined version of that page on the fly. Each HTTP request is serialized and sent to the AI, along with the full HTML of the last 3 pages you visited, allowing you to explore a vast, interconnected, alternate reality Internet where the only limit is your imagination.

#### (not) YouTube
A legally distinct parody of YouTube, which uses the fantastic homebrew application <a href="https://www.macflim.com/macflim2/">MacFlim</a> (created by Fred Stark) to encode video files as a series of dithered black and white frames.

#### Hackaday
A pared-down, text-only version of hackaday.com, complete with articles, comments, and search functionality.

#### npr.org
Serves articles from the text-only version of the site (```text.npr.org```) and transforms relative urls into absolute urls for compatibility with MacWeb 2.0.

#### wiby.me
Browse Wiby's collection of personal, handmade webpages (fixes an issue where clicking "surprise me..." would not redirect users to their final destination).

### Future Work
- more extensions for more sites
- presets targeting specific vintage machines/browsers
- wiki with how-to guides for different machines

Happy Surfing 😎