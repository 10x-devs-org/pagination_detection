"""
pagination
==========
Detects pagination on any MRP (Multiple Record Page) URL.

Usage as a package:
    from pagination import analyse_url
    result = await analyse_url("https://example.com/products")

Usage from CLI:
    python -m pagination <url>
    python -m pagination --file urls.txt --pretty
"""

from .analyse import analyse_url
from .cli import main
from .parsing import parse_pagination

__all__ = ["analyse_url", "main", "parse_pagination"]
