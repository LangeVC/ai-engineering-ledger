from __future__ import annotations

import re
from pathlib import Path

from ael.cli import build_parser


def test_cli_exposes_ui_command() -> None:
    args = build_parser().parse_args(["ui", "--no-open", "--port", "0"])
    assert args.command == "ui"
    assert args.no_open is True


def test_assets_are_local_and_accessible_by_contract() -> None:
    html = Path("ael/assets/index.html").read_text()
    css = Path("ael/assets/app.css").read_text()
    assert '<html lang="en">' in html
    assert 'class="skip"' in html
    assert html.count("<main") == 1
    assert "aria-label=" in html and "aria-live=" in html
    assert "<caption>" in html and "<th>" in html
    assert ":focus-visible" in css
    assert "prefers-reduced-motion" in css
    assert not re.search(r"https?://", html + css)


def test_package_data_declares_browser_assets() -> None:
    pyproject = Path("pyproject.toml").read_text()
    assert '"ael.assets" = ["*.html", "*.css", "*.js"]' in pyproject


def test_source_form_keeps_intrinsic_height() -> None:
    css = Path("ael/assets/app.css").read_text()
    source_layout = re.search(r"\.source-layout\{([^}]*)\}", css)
    assert source_layout is not None
    assert "align-items:start" in source_layout.group(1)

    responsive_layout = re.search(r"@media\(max-width:900px\)\{([^}]*)", css)
    assert responsive_layout is not None
    assert ".source-layout{grid-template-columns:1fr" in responsive_layout.group(1)
