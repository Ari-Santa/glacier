"""Render Glacier's app icon (faceted ice peak on deep navy) into glacier/assets/."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "glacier" / "assets"
S = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def vertical_gradient(size, top, bottom):
    w, h = size
    g = Image.new("RGBA", size)
    d = ImageDraw.Draw(g)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=lerp(top, bottom, y / max(h - 1, 1)))
    return g


def poly_fill(base: Image.Image, pts, top, bottom, alpha=255):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    box = (int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1)
    grad = vertical_gradient((box[2] - box[0], box[3] - box[1]), top + (alpha,), bottom + (alpha,))
    mask = Image.new("L", grad.size, 0)
    ImageDraw.Draw(mask).polygon([(x - box[0], y - box[1]) for x, y in pts], fill=255)
    base.paste(grad, box[:2], mask)


def render() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # Background tile
    bg = vertical_gradient((S, S), (14, 32, 58, 255), (6, 12, 24, 255))
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((160, -120, 864, 560), fill=(90, 190, 255, 120))
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    bg = Image.alpha_composite(bg, glow)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=210, fill=255)
    img.paste(bg, (0, 0), mask)

    # Back peaks
    poly_fill(img, [(120, 780), (360, 400), (560, 780)], (120, 170, 215), (40, 70, 110))
    poly_fill(img, [(520, 780), (760, 450), (920, 780)], (110, 160, 210), (35, 65, 105))
    # Main peak: left (lit) and right (shadow) faces
    apex = (512, 210)
    poly_fill(img, [apex, (230, 800), (512, 800)], (245, 252, 255), (150, 210, 245))
    poly_fill(img, [apex, (512, 800), (800, 800)], (150, 205, 240), (55, 120, 185))
    # Ice facets / crevasses
    d = ImageDraw.Draw(img)
    d.polygon([apex, (450, 420), (512, 380)], fill=(255, 255, 255, 255))
    d.polygon([apex, (512, 380), (575, 450)], fill=(200, 232, 252, 255))
    d.line([(512, 380), (470, 560), (512, 800)], fill=(170, 220, 250, 255), width=10)
    d.line([(575, 450), (610, 620)], fill=(95, 160, 215, 255), width=10)
    d.line([(450, 420), (380, 600)], fill=(205, 235, 252, 255), width=8)
    # Water line + reflection
    refl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(refl)
    rd.polygon([(230, 800), (512, 960), (800, 800)], fill=(120, 200, 255, 70))
    refl = refl.filter(ImageFilter.GaussianBlur(6))
    img = Image.alpha_composite(img, refl)
    d = ImageDraw.Draw(img)
    d.line([(150, 800), (874, 800)], fill=(170, 225, 255, 255), width=12)
    # Clip everything to the tile
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    big = render()
    for size in (16, 22, 24, 32, 48, 64, 96, 128, 256, 512):
        big.resize((size, size), Image.LANCZOS).save(OUT / f"glacier-{size}.png")
    big.resize((512, 512), Image.LANCZOS).save(OUT / "glacier.png")
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
