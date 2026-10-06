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

The `CSS_UNSUPPORTED_*` lists in `presets/classilla/classilla.py` and `presets/powerfox/powerfox.py` are educated guesses; tune them against the real browser. Scripts are still stripped, and CSS translation is skipped for `WHITELISTED_DOMAINS`. To run the unit tests: `python -m unittest tests.test_css_utils tests.test_layout_utils` (needs `tinycss2`, `beautifulsoup4`, `html5lib`).

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