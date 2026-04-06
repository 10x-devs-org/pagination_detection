"""
helpers.py
==========
Small utility functions used by the analyse module.
"""

import re
from urllib.parse import urlparse


def _build_api_result(url, api_info, fetch_method):
    return {
        "url": url, "block_detected": False, "block_type": None,
        "fetch_method": fetch_method, "pagination_found": True,
        "pagination_type": "api", "pagination_key": api_info.get("pagination_key"),
        "max_page": None,
        "notes": f"Pagination via intercepted API: {api_info.get('url', '')}",
    }


def _write_debug(debug, url, body, suffix=""):
    if not debug or not body:
        return
    safe = re.sub(r"[^\w]", "_", urlparse(url).netloc + urlparse(url).path)[:60]
    fname = f"debug_{safe}{suffix}.html"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(body)
        import sys
        print(f"  [DEBUG] HTML -> {fname}", file=sys.stderr)
    except Exception as e:
        import sys
        print(f"  [DEBUG] Write failed: {e}", file=sys.stderr)
