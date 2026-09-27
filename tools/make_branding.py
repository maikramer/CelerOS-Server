#!/usr/bin/env python3
"""Gera os assets de marca do portal a partir de branding/ (artes originais).

Uso: python3 tools/make_branding.py   (raiz do repo)
Escreve em content/www/assets/: celeros_logo.png (web), celer_logo.png,
celeros_favicon.png, celer_favicon.png (128px). Rodar de novo se as artes
mudarem; as originais permanecem intocadas em branding/.
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "branding"
DST = ROOT / "content" / "www" / "assets"

# (origem, destino, largura maxima)
JOBS = [
    ("celeros_logo.png", "celeros_logo.png", 1080),
    ("celer_logo_principal.png", "celer_logo.png", 1080),
    ("celeros_favicon.png", "celeros_favicon.png", 128),
    ("celer_favicon.png", "celer_favicon.png", 128),
]


def main() -> None:
    DST.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name, max_w in JOBS:
        im = Image.open(SRC / src_name).convert("RGBA")
        if im.width > max_w:
            h = round(im.height * max_w / im.width)
            im = im.resize((max_w, h), Image.LANCZOS)
        out = DST / dst_name
        im.save(out, "PNG", optimize=True)
        print(f"{out.relative_to(ROOT)}  {im.size}  {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
