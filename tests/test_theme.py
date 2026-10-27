from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


def test_every_page_loads_the_theme_script_before_the_body():
    pages = sorted(STATIC.glob("*.html"))
    assert pages
    for page in pages:
        html = page.read_text(encoding="utf-8")
        assert '<script src="theme.js"></script>' in html, f"{page.name} does not load theme.js"
        assert html.index("theme.js") < html.index("</head>"), f"{page.name} loads theme.js after </head>"


def test_theme_choice_is_remembered_and_follows_the_os_until_then():
    js = (STATIC / "theme.js").read_text(encoding="utf-8")
    assert 'const KEY = "sinag_theme"' in js
    assert "localStorage.getItem(KEY)" in js and "localStorage.setItem(KEY" in js
    assert "prefers-color-scheme: dark" in js
    assert "data-theme" in js
    # Both options stay on screen with the same labels, so no text changes with the theme.
    assert '"Light"' in js and '"Dark"' in js
    # The script only edits the header control and the theme attribute, never page content.
    assert "document.body" not in js
    assert "replaceWith(" not in js and "removeChild(" not in js


def test_both_themes_exist_and_never_restyle_content():
    css = (STATIC / "site.css").read_text(encoding="utf-8")
    assert ':root[data-theme="dark"]' in css
    assert ':root[data-theme="light"]' not in css  # light is the default, not an override
    assert ':root:not([data-theme="light"])' in css
    # Theme changes must not recolour, hide or distort images and icons.
    assert "filter:" not in css
    assert "mix-blend-mode" not in css
    assert "object-fit:cover" in css  # images keep the same crop in both themes


def test_server_rendered_pages_use_the_same_theme():
    app = Path(__file__).resolve().parent.parent / "app"
    assert '<script src="/theme.js"></script>' in (app / "seo.py").read_text(encoding="utf-8")
    assert "<link rel='stylesheet' href='/site.css'>" in (app / "api.py").read_text(encoding="utf-8")
    assert "<script src='/theme.js'></script>" in (app / "api.py").read_text(encoding="utf-8")
