# Brand assets

- `logo.svg` is drawn by hand. It is the source of the other images.
- `banner.svg` is the header of the README.
- `social.svg` and `social.png` are the social preview (1280 × 640). Upload
  `social.png` by hand in the repository settings. GitHub has no API for this.

Do not edit `banner.svg`, `social.svg` or `social.png`. Change `logo.svg` or the
words in `scripts/build-brand.py`, then run:

```bash
uv run --no-project --with fonttools --with cairosvg python scripts/build-brand.py
```

The script draws the text as outlines, so the images look the same on every machine.
The letter shapes come from DejaVu Sans, which has a free license that allows this.
