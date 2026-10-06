# Preset for PowerFox on Mac OS X 10.4 Tiger (a Basilisk/UXP-based browser with a modern, Firefox ~52-era
# engine: flexbox, grid, var(), calc(), rgba(), most of ES6).
#
# The engine handles most modern CSS natively, so this preset only translates the newer syntax it is
# unlikely to know (min()/max()/clamp(), lab/oklch/color-mix, newer viewport units, `inset`, :is()/:where()/
# :has(), @layer/@container, CSS nesting) and keeps everything else as the site wrote it.
# The lists are educated guesses; tune them against the real browser.
# Scripts are still stripped for now.

SIMPLIFY_HTML = True

TAGS_TO_UNWRAP = [
	"noscript",
]

TAGS_TO_STRIP = [
	"script",
]

ATTRIBUTES_TO_STRIP = [
	"integrity",
	"loading",
]

CAN_RENDER_INLINE_IMAGES = True
RESIZE_IMAGES = True
MAX_IMAGE_WIDTH = 1600
MAX_IMAGE_HEIGHT = 1200
CONVERT_IMAGES = False
CONVERT_IMAGES_TO_FILETYPE = None
DITHERING_ALGORITHM = None

CSS_MODE = "downlevel"

# Size of the browser window's content area, used to evaluate @media queries and resolve calc()/min()/max().
CSS_VIEWPORT_WIDTH = 1280
CSS_VIEWPORT_HEIGHT = 960

CSS_UNSUPPORTED_FEATURES = [
	"minmax",              # min() / max() / clamp()
	"modern-color",        # lab/lch/oklch/color-mix/hwb/light-dark are dropped
	"new-viewport-units",  # dvh/svh/lvh/... -> px
	"inset",               # inset -> top/right/bottom/left
]

CSS_UNSUPPORTED_PROPERTIES = [
	"aspect-ratio", "backdrop-filter", "accent-color", "color-scheme", "text-wrap", "content-visibility",
	"overscroll-behavior", "scroll-snap-type", "scroll-snap-align", "text-decoration-thickness",
	"text-underline-offset", "contain", "container", "container-type", "container-name",
]

CSS_UNSUPPORTED_VALUES = {
	"display": ["flow-root"],
}

CSS_UNSUPPORTED_SELECTORS = [
	r":is\(", r":where\(", r":has\(", r":focus-visible", r":matches\(",
	r":not\([^)]*[,\s>+~]",   # complex selectors inside :not()
	r"::backdrop", r"::marker",
]

CSS_PROPERTY_RENAMES = {
	"gap": ["grid-gap"],
	"row-gap": ["grid-row-gap"],
}

CSS_STRIP_AT_RULES = [
	"property", "container", "starting-style", "scope", "font-palette-values", "view-transition",
]

WEB_SIMULATOR_PROMPT_ADDENDUM = """<formatting>
The user is accessing these pages from a G4 iMac running Mac OS X 10.4 Tiger with PowerFox, a Firefox-derived browser with a modern engine (roughly Firefox 52 era).
It supports HTML5, CSS3 including flexbox and grid, and most of ES6 JavaScript, but not newer features such as container queries, :has(), CSS nesting, or optional chaining in some versions. Prefer simple, robust markup.
Screen size is about 1280 x 960.
</formatting>"""
