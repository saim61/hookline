"""Filesystem locations of packaged non-Python assets.

Resolved relative to this module rather than to the working directory, so the dashboard
renders the same whether the app is started from the repo root, from `src/`, or from an
installed wheel inside a container.
"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
