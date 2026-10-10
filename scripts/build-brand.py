#!/usr/bin/env python3
"""
Build the banner and the social preview from assets/logo.svg.

The text is turned into outlines, so the images look the same on every machine and
need no installed font. Run it when you change the logo or the words:

    uv run --no-project --with fonttools --with cairosvg python scripts/build-brand.py

It writes assets/banner.svg, assets/social.svg and assets/social.png. The PNG is the
file to upload by hand as the social preview in the repository settings. The script
needs DejaVu Sans. Use --font-dir if your fonts are not in /usr/share/fonts/truetype/dejavu.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
NAVY, MINT, GREEN = "#1F2A44", "#E9FBF7", "#2C8F80"

TITLE = "dsh-workers-mcp"
TAGLINE = "Drive long-lived dsh sessions as persistent workers."
SUBLINE = "An MCP server in plain Python. No dependencies."
DESCRIPTION = f"{TITLE}: {TAGLINE[0].lower()}{TAGLINE[1:-1]}"


def text_path(text: str, font_file: Path, size: float, x: float, y: float, fill: str, *,
              anchor: str = "start", outline: float = 0.0) -> tuple[str, float]:
    """Return (svg group, width) for the text drawn as outlines."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.ttLib import TTFont

    font = TTFont(font_file)
    glyphs, cmap, advances = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]
    scale = size / font["head"].unitsPerEm
    shapes, cursor = [], 0
    for char in text:
        name = cmap[ord(char)]
        pen = SVGPathPen(glyphs)
        glyphs[name].draw(pen)
        if pen.getCommands():
            shapes.append(f'<path transform="translate({cursor} 0)" d="{pen.getCommands()}"/>')
        cursor += advances[name][0]
    width = cursor * scale
    left = {"start": x, "middle": x - width / 2, "end": x - width}[anchor]
    stroke = ""
    if outline:
        stroke = f' stroke="{fill}" stroke-width="{outline / scale:.1f}" stroke-linejoin="round"'
    place = f"translate({left:.1f} {y}) scale({scale:.5f} {-scale:.5f})"
    group = f'<g fill="{fill}"{stroke} transform="{place}">' + "".join(shapes) + "</g>"
    return group, width


def logo_at(inner: str, x: float, y: float, side: float) -> str:
    return f'<g transform="translate({x} {y}) scale({side / 512:.4f})">{inner}</g>'


def frame(width: int, height: int, body: str, radius: int) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" role="img" aria-labelledby="t">\n  <title id="t">{DESCRIPTION}</title>\n'
            f'  <rect x="4" y="4" width="{width - 8}" height="{height - 8}" rx="{radius}" fill="{MINT}" '
            f'stroke="{NAVY}" stroke-width="7"/>\n  {body}\n</svg>\n')


def banner(inner: str, bold: Path, regular: Path) -> str:
    title, width = text_path(TITLE, bold, 78, 330, 138, NAVY, outline=2.5)
    tagline, _ = text_path(TAGLINE, regular, 27, 334, 196, NAVY)
    subline, _ = text_path(SUBLINE, regular, 22, 334, 238, GREEN)
    if 330 + width > 1170:
        raise SystemExit("the title does not fit in the banner")
    return frame(1200, 300, "\n  ".join([logo_at(inner, 34, 30, 240), title, tagline, subline]), 44)


def social(inner: str, bold: Path, regular: Path) -> str:
    title, title_width = text_path(TITLE, bold, 92, 640, 468, NAVY, anchor="middle", outline=3)
    tagline, tagline_width = text_path(TAGLINE, regular, 32, 640, 530, NAVY, anchor="middle")
    subline, _ = text_path(SUBLINE, regular, 26, 640, 580, GREEN, anchor="middle")
    if max(title_width, tagline_width) > 1200:
        raise SystemExit("the text does not fit in the social preview")
    # A full-size background and square corners: no transparent pixels in the PNG.
    background = f'<rect x="0" y="0" width="1280" height="640" fill="{MINT}"/>'
    parts = [background, logo_at(inner, 490, 36, 300), title, tagline, subline]
    return frame(1280, 640, "\n  ".join(parts), 0)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--font-dir", type=Path, default=Path("/usr/share/fonts/truetype/dejavu"))
    args = parser.parse_args(argv)
    bold, regular = args.font_dir / "DejaVuSans-Bold.ttf", args.font_dir / "DejaVuSans.ttf"
    for font in (bold, regular):
        if not font.exists():
            raise SystemExit(f"missing font: {font} (use --font-dir)")
    match = re.search(r"<g stroke=.*</g>", (ASSETS / "logo.svg").read_text(), re.S)
    if not match:
        raise SystemExit("assets/logo.svg has no drawing group")
    inner = match.group(0)
    (ASSETS / "banner.svg").write_text(banner(inner, bold, regular))
    (ASSETS / "social.svg").write_text(social(inner, bold, regular))
    import cairosvg
    cairosvg.svg2png(url=str(ASSETS / "social.svg"), write_to=str(ASSETS / "social.png"))
    print("wrote assets/banner.svg, assets/social.svg and assets/social.png")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
