"""The brand assets exist, parse, and are the ones that the README shows."""
from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SVGS = ["logo.svg", "banner.svg", "social.svg"]


@pytest.mark.parametrize("name", SVGS)
def test_each_svg_is_well_formed_and_has_a_title(name: str) -> None:
    root = ET.parse(ASSETS / name).getroot()
    assert root.tag.endswith("svg") and root.get("viewBox")
    assert any(el.tag.endswith("title") and (el.text or "").strip() for el in root.iter()), name


def test_the_social_preview_has_the_size_that_github_asks_for() -> None:
    data = (ASSETS / "social.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (1280, 640)
    assert len(data) < 1_000_000          # the limit for a social preview


def test_the_readme_shows_images_that_exist() -> None:
    readme = (ROOT / "README.md").read_text()
    targets = re.findall(r"\]\((assets/[^)\s]+)\)|src=\"(assets/[^\"]+)\"", readme)
    paths = [a or b for a, b in targets]
    assert "assets/banner.svg" in paths
    assert all((ROOT / path).exists() for path in paths), paths
