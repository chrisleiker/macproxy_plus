# Preset for Classilla on Mac OS 9 (a patched Mozilla 1.3.1 / "Clecko" engine; CSS2.1-era, JS 1.5).
#
# CSS is translated by utils/css_utils.py: pages keep their styling, but anything this engine
# cannot parse (var(), calc(), rgba(), flexbox, grid, gradients, ...) is resolved or dropped.
# The feature lists below are educated guesses from the engine's age. Tune them against the real
# browser: if something looks wrong, add the offending property/selector here.
# Scripts are still stripped for now.

SIMPLIFY_HTML = True

TAGS_TO_UNWRAP = [
	"noscript",
]

# <link> and <style> are kept (and rewritten) so that sites keep their styling
TAGS_TO_STRIP = [
	"script",
	"source",
]

ATTRIBUTES_TO_STRIP = [
	"onclick",
	"onload",
	"onerror",
	"onmouseover",
	"onmouseout",
	"onfocus",
	"onblur",
	"onchange",
	"onsubmit",
	"integrity",
	"srcset",
	"sizes",
	"loading",
]

CAN_RENDER_INLINE_IMAGES = True
RESIZE_IMAGES = True
MAX_IMAGE_WIDTH = 1024
MAX_IMAGE_HEIGHT = 768
CONVERT_IMAGES = True
CONVERT_IMAGES_TO_FILETYPE = "png"
DITHERING_ALGORITHM = None

CSS_MODE = "downlevel"

# Size of the browser window's content area, used to evaluate @media queries and vw/vh units.
# A 17" iMac G4 is 1024x768; the 20" model is 1280x960.
CSS_VIEWPORT_WIDTH = 1024
CSS_VIEWPORT_HEIGHT = 768

CSS_UNSUPPORTED_FEATURES = [
	"var",                 # custom properties are resolved on the server
	"calc",                # calc() / -moz-calc() are evaluated on the server
	"minmax",              # min() / max() / clamp()
	"color-functions",     # rgba()/hsl()/hsla() -> opaque #rrggbb (blended on white)
	"hexalpha",            # #rrggbbaa / #rgba
	"rem",                 # rem -> px
	"viewport-units",      # vw/vh/vmin/vmax -> px
	"new-viewport-units",  # dvh/svh/lvh/... -> px
	"gradients",           # replaced by the first color stop
	"modern-color",        # lab/lch/oklch/color-mix/... are dropped
	"double-colon",        # ::before -> :before
	"logical-properties",  # margin-inline-start -> margin-left, etc.
	"inset",               # inset -> top/right/bottom/left
]

CSS_UNSUPPORTED_PROPERTIES = [
	# layout
	"flex", "flex-basis", "flex-direction", "flex-flow", "flex-grow", "flex-shrink", "flex-wrap",
	"align-items", "align-self", "align-content", "justify-content", "justify-items", "justify-self",
	"place-items", "place-content", "place-self", "order", "gap", "row-gap", "column-gap",
	"grid", "grid-area", "grid-auto-columns", "grid-auto-flow", "grid-auto-rows", "grid-column",
	"grid-column-end", "grid-column-start", "grid-gap", "grid-row", "grid-row-end", "grid-row-start",
	"grid-template", "grid-template-areas", "grid-template-columns", "grid-template-rows",
	"aspect-ratio", "object-fit", "object-position", "contain", "content-visibility",
	"scroll-behavior", "scroll-snap-type", "scroll-snap-align", "scroll-margin", "scroll-padding",
	"overscroll-behavior", "writing-mode", "touch-action",
	# visual effects
	"transform", "transform-origin", "transform-style", "perspective", "backface-visibility",
	"transition", "transition-delay", "transition-duration", "transition-property", "transition-timing-function",
	"animation", "animation-name", "animation-duration", "animation-delay", "animation-fill-mode",
	"animation-iteration-count", "animation-timing-function", "animation-direction", "animation-play-state",
	"box-shadow", "text-shadow", "filter", "backdrop-filter", "clip-path", "mask", "mask-image",
	"mix-blend-mode", "isolation", "will-change", "caret-color", "accent-color", "color-scheme",
	"appearance", "user-select", "pointer-events", "resize",
	# text
	"text-overflow", "text-wrap", "word-break", "overflow-wrap", "word-wrap", "hyphens", "tab-size",
	"line-clamp", "text-rendering", "text-size-adjust", "font-display", "font-feature-settings",
	"font-variation-settings", "font-kerning", "font-optical-sizing", "font-smooth",
	"image-rendering", "text-decoration-thickness", "text-underline-offset",
]

CSS_UNSUPPORTED_VALUES = {
	"display": ["flex", "inline-flex", "grid", "inline-grid", "contents", "flow-root",
				"-webkit-box", "-webkit-flex", "-ms-flexbox", "-ms-grid", "list-item-flow"],
	"position": ["sticky", "-webkit-sticky"],
	"overflow": ["clip", "overlay"],
	"overflow-x": ["clip", "overlay"],
	"overflow-y": ["clip", "overlay"],
	"cursor": ["grab", "grabbing", "zoom-in", "zoom-out"],
}

CSS_UNSUPPORTED_SELECTORS = [
	r":not\(", r":is\(", r":where\(", r":has\(", r":matches\(",
	r":nth-", r":last-child", r":only-child", r":first-of-type", r":last-of-type", r":only-of-type",
	r":empty", r":target", r":checked", r":disabled", r":enabled", r":focus-within", r":focus-visible",
	r":placeholder-shown", r":read-only", r":required", r":optional", r":valid", r":invalid", r":root\b",
	r"::placeholder", r"::selection", r"::marker", r"::backdrop", r"::-webkit-", r":-webkit-", r":-ms-",
	r"\[[^\]]*[\^$*~|]=",   # CSS3 attribute selectors
	r"~",                   # general sibling combinator
]

# Properties to rename (the list may include the original name). Gecko of this era used -moz- prefixes.
CSS_PROPERTY_RENAMES = {
	"border-radius": ["-moz-border-radius"],
	"border-top-left-radius": ["-moz-border-radius-topleft"],
	"border-top-right-radius": ["-moz-border-radius-topright"],
	"border-bottom-left-radius": ["-moz-border-radius-bottomleft"],
	"border-bottom-right-radius": ["-moz-border-radius-bottomright"],
	"box-sizing": ["-moz-box-sizing"],
	"opacity": ["-moz-opacity", "opacity"],
}

CSS_STRIP_AT_RULES = [
	"font-face", "keyframes", "-webkit-keyframes", "-moz-keyframes", "counter-style", "property",
	"namespace", "charset", "container", "page", "viewport", "-ms-viewport", "document", "-moz-document",
	"font-feature-values", "scope", "starting-style",
]

WEB_SIMULATOR_PROMPT_ADDENDUM = """<formatting>
The user is accessing these pages from a G4 iMac running Mac OS 9 with Classilla, a browser based on Mozilla 1.3.1.
Its HTML and CSS support is roughly HTML 4.01 and CSS 2.1. Do not use flexbox, grid, CSS variables, calc(), rgba(), CSS transforms, transitions or animations, or <canvas>. Use tables or floats for layout, and plain <style> blocks or inline styles with simple hex colors.
Do not rely on JavaScript; keep every interaction a normal link or form submission.
Screen size is about 1024 x 768. Always format <br> and <hr> without a trailing slash.
</formatting>"""
