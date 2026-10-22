"""Sphinx configuration for the DJ Snake user documentation."""

project = "DJ Snake"
author = "Theroxat"
copyright = "2026, Theroxat and contributors"

extensions = ["myst_parser", "sphinx_copybutton"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build"]

myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

html_theme = "furo"
html_title = "DJ Snake"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_theme_options = {
    "source_repository": "https://github.com/Theroxat/DJ_Snake/",
    "source_branch": "main",
    "source_directory": "docs/source/",
    # Brand accent: the red of the README release badge (#ff2c41), darkened
    # a little in light mode for text contrast.
    "light_css_variables": {
        "color-brand-primary": "#d9132a",
        "color-brand-content": "#d9132a",
    },
    "dark_css_variables": {
        "color-brand-primary": "#ff5a6b",
        "color-brand-content": "#ff5a6b",
    },
}
