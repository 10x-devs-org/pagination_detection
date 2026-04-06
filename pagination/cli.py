"""
cli.py
======
Command-line interface entry point.
"""

import argparse
import asyncio
import json
import os
import sys
import time

from .analyse import analyse_url
from .dependencies import (
    CAPTCHA_SOLVER_AVAILABLE,
    CURL_CFFI_AVAILABLE,
    PLAYWRIGHT_AVAILABLE,
    STEALTH_AVAILABLE,
)


async def main():
    start_time = time.time()

    parser = argparse.ArgumentParser(
        description="Detect pagination on MRP URLs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--file", "-f", default="urls.txt")
    parser.add_argument("--output", "-o", default="output.json")
    parser.add_argument("--stats", "-s", default="statistics.json",
                        help="File to save execution statistics")
    parser.add_argument("--pretty", action="store_true", default=True)
    parser.add_argument("--no-playwright", dest="no_playwright", action="store_true")
    parser.add_argument("--proxy", default=None,
                        help="e.g. http://user:pass@host:port")
    parser.add_argument("--captcha-key", dest="captcha_key", default=None,
                        help="2captcha API key (or set CAPTCHA_API_KEY env var)")
    parser.add_argument("--scraper-key", dest="scraper_key", default=None,
                        help="ScraperAPI key for fetching HTML via proxy")
    parser.add_argument("--debug", action="store_true",
                        help="Write fetched HTML to debug_*.html")
    args = parser.parse_args()

    urls = list(args.urls)
    if args.file and os.path.exists(args.file):
        with open(args.file, "r", encoding="utf-8") as fh:
            urls += [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    elif args.file and not os.path.exists(args.file) and args.file != "urls.txt":
        # Only warn if the user explicitly provided a file that doesn't exist
        print(f"  [!] Warning: File not found: {args.file}", file=sys.stderr)
    if not urls:
        parser.print_help()
        sys.exit(1)

    captcha_key = args.captcha_key or os.environ.get("CAPTCHA_API_KEY")
    scraper_key = args.scraper_key or os.environ.get("SCRAPER_API_KEY")
    proxy = {"server": args.proxy} if args.proxy else None

    print(f"  curl_cffi (TLS) : {CURL_CFFI_AVAILABLE}", file=sys.stderr)
    print(f"  Playwright      : {PLAYWRIGHT_AVAILABLE}", file=sys.stderr)
    print(f"  Stealth         : {STEALTH_AVAILABLE}", file=sys.stderr)
    print(f"  CAPTCHA solver  : {CAPTCHA_SOLVER_AVAILABLE} "
          f"(key={'set' if captcha_key else 'not set'})", file=sys.stderr)
    if not CURL_CFFI_AVAILABLE:
        print("  [!] pip install curl_cffi", file=sys.stderr)
    if not STEALTH_AVAILABLE:
        print("  [!] pip install playwright-stealth", file=sys.stderr)
    if not CAPTCHA_SOLVER_AVAILABLE:
        print("  [!] pip install 2captcha-python", file=sys.stderr)
    print("", file=sys.stderr)

    stats = {
        "total_urls": len(urls),
        "success": 0,
        "blocked": 0,
        "failed": 0,
        "success_rate_percentage": 0.0,
        "block_types": {},
        "fetch_methods_used": {},
        "pagination_types_found": {},
        "duration_seconds": 0.0,
    }

    results = []
    for url in urls:
        print(f"  Analysing: {url}", file=sys.stderr)
        result = await analyse_url(url, no_playwright=args.no_playwright,
                                   proxy=proxy, captcha_api_key=captcha_key,
                                   debug=args.debug, scraper_key=scraper_key)
        results.append(result)

        if result.get("pagination_found") and result.get("pagination_type") != "none":
            stats["success"] += 1
        elif result.get("block_detected"):
            stats["blocked"] += 1
            bt = result.get("block_type") or "unknown"
            stats["block_types"][bt] = stats["block_types"].get(bt, 0) + 1
        else:
            stats["failed"] += 1

        fm = result.get("fetch_method", "unknown")
        stats["fetch_methods_used"][fm] = stats["fetch_methods_used"].get(fm, 0) + 1
        if result.get("pagination_found"):
            pt = result.get("pagination_type", "unknown")
            stats["pagination_types_found"][pt] = stats["pagination_types_found"].get(pt, 0) + 1

        print(
            f"  -> type={result['pagination_type']} | key={result['pagination_key']} "
            f"| max={result['max_page']} | blocked={result['block_detected']} "
            f"| method={result['fetch_method']}",
            file=sys.stderr,
        )

    if stats["total_urls"] > 0:
        stats["success_rate_percentage"] = round(
            (stats["success"] / stats["total_urls"]) * 100, 2
        )
    stats["duration_seconds"] = round(time.time() - start_time, 2)

    try:
        with open(args.stats, "w", encoding="utf-8") as fs:
            json.dump(stats, fs, indent=4)
        print(f"\nStatistics written to {args.stats}", file=sys.stderr)
    except Exception as e:
        print(f"\nFailed to write statistics: {e}", file=sys.stderr)

    indent = 2 if args.pretty else None
    out = json.dumps(results if len(results) > 1 else results[0], indent=indent)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"Results written to {args.output}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    asyncio.run(main())
