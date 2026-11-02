#!/usr/bin/env python3
"""
Gera os ícones do PWA a partir do mesmo vocabulário visual do dashboard:
gradiente teal→azul (var(--accent) → var(--accent2)) com raio branco.

Saída (commitadas no repo — este script é só de DEV no PC, a placa nunca
importa Pillow):
  src/icons/icon-512.png         512×512, cantos arredondados + alpha (Android)
  src/icons/apple-touch-icon.png 180×180, quadrado full-bleed (iOS arredonda sozinho)

Run with:  python3 scripts/gen_pwa_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

SRC = Path(__file__).resolve().parent.parent / "src" / "icons"
TEAL = (0, 212, 170)    # var(--accent)
BLUE = (59, 130, 246)   # var(--accent2)
DARK = (10, 14, 20)     # var(--bg)

# Raio (lightning bolt) em coords normalizadas da caixa 0..1
BOLT = [(0.57, 0.12), (0.30, 0.55), (0.47, 0.55),
        (0.40, 0.88), (0.72, 0.44), (0.53, 0.44)]


def gradient(size: int) -> Image.Image:
    """Gradiente diagonal teal→azul (t = (x+y) / (2·size))."""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TEAL, BLUE))
    return img


def bolt(size: int) -> Image.Image:
    """Raio branco em canvas transparente."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.polygon([(int(x * size), int(y * size)) for x, y in BOLT], fill="white")
    return layer


def rounded(img: Image.Image, radius: int) -> Image.Image:
    """Recorta cantos arredondados (mantém alpha)."""
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width - 1, img.height - 1],
                                           radius=radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def make_icon(size: int, radius: int) -> Image.Image:
    base = gradient(size).convert("RGBA")
    base.alpha_composite(bolt(size))
    return rounded(base, radius) if radius else base


def main():
    SRC.mkdir(parents=True, exist_ok=True)
    icon512 = make_icon(512, 96)
    icon512.save(SRC / "icon-512.png")
    # apple-touch: quadrado cheio, sem alpha — o iOS aplica a própria máscara
    touch = gradient(180).convert("RGBA")
    touch.alpha_composite(bolt(180))
    touch.convert("RGB").save(SRC / "apple-touch-icon.png")
    print(f"OK: {SRC/'icon-512.png'} e {SRC/'apple-touch-icon.png'}")


if __name__ == "__main__":
    main()
