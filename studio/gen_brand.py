"""Generate Shipcast brand assets: mark, wordmark (text as paths), favicons.
Run: uv run --with fonttools --with brotli --with cairosvg --with pillow python gen_brand.py <font.woff2> <outdir>"""
import io
import sys

import cairosvg
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image

font_path, out = sys.argv[1], sys.argv[2]
ORANGE_A, ORANGE_B = "#FF7A2F", "#E8400C"

# one point casting out to many: the product in one glyph
MARK_BODY = f"""<defs><linearGradient id="sg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{ORANGE_A}"/><stop offset="1" stop-color="{ORANGE_B}"/></linearGradient></defs>
<rect width="128" height="128" rx="34" fill="url(#sg)"/>
<path d="M38 64L90 33M38 64L97 64M38 64L90 95" stroke="#fff" stroke-width="9" stroke-linecap="round" fill="none"/>
<circle cx="38" cy="64" r="14" fill="#fff"/>
<circle cx="90" cy="33" r="8.5" fill="#fff"/><circle cx="97" cy="64" r="8.5" fill="#fff"/><circle cx="90" cy="95" r="8.5" fill="#fff"/>"""


def svg(w, h, body):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">{body}</svg>\n'


def text_path(text, height, x0, cy):
    """Glyph outlines of `text`, scaled so the ink is `height` tall, starting at x0, vertically centred on cy."""
    f = TTFont(font_path)
    gs, cmap = f.getGlyphSet(), f.getBestCmap()
    names = [cmap[ord(c)] for c in text]
    bp, x = BoundsPen(gs), 0
    for n in names:  # ink bounds of the whole run in font units
        gs[n].draw(TransformPen(bp, (1, 0, 0, 1, x, 0)))
        x += gs[n].width
    xmin, ymin, xmax, ymax = bp.bounds
    s = height / (ymax - ymin)
    pen, x = SVGPathPen(gs), 0
    for n in names:  # flip y, scale, place
        gs[n].draw(TransformPen(pen, (s, 0, 0, -s, x0 + (x - xmin) * s, cy + (ymax + ymin) / 2 * s)))
        x += gs[n].width
    return pen.getCommands(), (xmax - xmin) * s, cy + (ymax + ymin) / 2 * s  # path, width, baseline y


mark = svg(128, 128, MARK_BODY)
word, word_w, baseline = text_path("shipcast", 78, 160, 64)
dot_x = 160 + word_w + 8
W = int(dot_x + 22)
wordmark = svg(W, 128, f'<g>{MARK_BODY}</g><path d="{word}" fill="#fff"/>'
                       f'<circle cx="{dot_x + 9:.1f}" cy="{baseline - 9:.1f}" r="9" fill="{ORANGE_A}"/>')

files = {"shipcast-mark.svg": mark, "shipcast-text.svg": wordmark,
         # Postiz's own file names, so anything that loads them by path gets Shipcast
         "logo.svg": mark, "postiz.svg": mark, "logo-text.svg": wordmark, "postiz-text.svg": wordmark}
for name, body in files.items():
    open(f"{out}/{name}", "w").write(body)

png = lambda size: Image.open(io.BytesIO(cairosvg.svg2png(bytestring=mark.encode(), output_width=size, output_height=size)))
png(48).save(f"{out}/favicon.png")
png(48).save(f"{out}/postiz-fav.png")
png(64).save(f"{out}/favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
png(512).save(f"{out}/shipcast-512.png")
cairosvg.svg2png(bytestring=wordmark.encode(), write_to=f"{out}/_preview-wordmark.png", output_height=128,
                 background_color="#1A1919")

assert W > 400 and "<path" in wordmark and Image.open(f"{out}/favicon.ico").size[0] >= 16
print("wordmark width", W, "->", sorted(files) + ["favicon.ico", "favicon.png", "postiz-fav.png"])
