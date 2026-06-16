"""
Generate the favicon raster assets (favicon.ico, apple-touch-icon.png) from the
brand palette, so they match static/favicon.svg. Run after changing the design:

    venv/bin/python gen_favicon.py

The SVG is hand-maintained; this script keeps the raster fallbacks in sync.
"""

from PIL import Image, ImageDraw

PURPLE = (167, 139, 250)   # --accent
PINK   = (244, 114, 182)   # --pink
GREEN  = (74, 222, 128)    # --green
WHITE  = (255, 255, 255)


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(n):
    img = Image.new('RGB', (n, n))
    px = img.load()
    for y in range(n):
        for x in range(n):
            t = (x + y) / (2 * (n - 1)) if n > 1 else 0
            px[x, y] = _lerp(PURPLE, PINK, t)
    return img


def icon(n, rounded=True):
    base = _gradient(n).convert('RGBA')
    if rounded:
        mask = Image.new('L', (n, n), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, n - 1, n - 1],
                                               radius=round(0.22 * n), fill=255)
        base.putalpha(mask)
    d = ImageDraw.Draw(base)
    x0, y0, x1, y1 = 0.31 * n, 0.19 * n, 0.69 * n, 0.81 * n   # the white "card"
    d.rounded_rectangle([x0, y0, x1, y1], radius=round(0.08 * n), fill=WHITE)
    # Green "art window" framed in the top half, like a Magic card.
    m = 0.05 * n
    d.rounded_rectangle([x0 + m, y0 + m, x1 - m, y0 + 0.42 * (y1 - y0)],
                        radius=round(0.03 * n), fill=GREEN)
    return base


def main():
    icon(64).save('static/favicon.ico', sizes=[(16, 16), (32, 32), (48, 48)])
    icon(180, rounded=False).convert('RGB').save('static/apple-touch-icon.png')
    print('Wrote static/favicon.ico and static/apple-touch-icon.png')


if __name__ == '__main__':
    main()
