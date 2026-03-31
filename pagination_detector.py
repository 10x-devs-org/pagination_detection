"""
pagination_detector.py
======================
Detects pagination on any MRP (Multiple Record Page) URL.

Strategy:
  1. Try plain requests with a full browser header bundle.
  2. Detect hard blocks (403/429/503, Cloudflare signals).
  3. If JS-rendered, fall back to Playwright (networkidle).
  4. For pure-SPA pages, intercept XHR/fetch to find API-level pagination.
  5. Return a structured JSON result.

Usage:
  python pagination_detector.py <url1> [url2 ...]
  python pagination_detector.py --file urls.txt
"""

import argparse
import asyncio
import json
import re
import sys
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

import requests
from bs4 import BeautifulSoup

# ── Optional Playwright import ──────────────────────────────────────────────
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── Constants ────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

CLOUDFLARE_SIGNALS = [
    "just a moment",
    "checking your browser",
    "cf-ray",
    "cloudflare",
    "__cf_bm",
    "ray id",
]

JS_FRAMEWORK_MARKERS = [
    "__NEXT_DATA__",
    "ng-app",
    "data-reactroot",
    "__vue__",
    "data-react-helmet",
    "nuxt",
    "_app.js",
]

PAGINATION_PARAM_NAMES = [
    "page", "p", "pg", "paged", "pagenum", "pageno",
    "start", "offset", "from", "skip",
    "limit", "per_page", "pageSize", "page_size",
    "currentPage", "current_page",
]

PAGINATION_SELECTORS = [
    ".pagination", ".pager", ".paginator",
    "[aria-label*='pagination']", "[aria-label*='Pagination']",
    "nav ul", ".page-numbers", ".pages",
    "[class*='paginat']", "[class*='pageNav']",
    "[class*='page-nav']", "[id*='pagination']",
]

PAGE_TEXT_PATTERNS = [
    # "Page 3 of 47"
    r"[Pp]age\s+\d+\s+of\s+(\d+)",
    # "Showing 1-20 of 400 results"
    r"[Ss]howing\s+[\d,]+[-–]\s*[\d,]+\s+of\s+([\d,]+)",
    # "400 results"
    r"([\d,]+)\s+results?",
    # "1 of 47 pages"
    r"\d+\s+of\s+(\d+)\s+pages?",
]

API_PAGINATION_PARAMS = set(PAGINATION_PARAM_NAMES)


# ═══════════════════════════════════════════════════════════════════════════
# BLOCK DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def detect_block(status_code: int, headers: dict, body: str) -> tuple[bool, str | None]:
    """Return (block_detected, block_type)."""
    if status_code in (403, 429, 503):
        body_lower = body.lower()
        if any(sig in body_lower or sig in str(headers).lower() for sig in CLOUDFLARE_SIGNALS):
            return True, "cloudflare"
        return True, "hard_block"

    body_lower = body.lower()
    cf_header = headers.get("cf-ray") or headers.get("CF-RAY")
    if cf_header or "just a moment" in body_lower or "checking your browser" in body_lower:
        return True, "cloudflare"

    return False, None


def is_js_rendered(body: str) -> bool:
    """Return True if any JS framework marker is found."""
    return any(marker in body for marker in JS_FRAMEWORK_MARKERS)


# ═══════════════════════════════════════════════════════════════════════════
# PAGINATION PARSING
# ═══════════════════════════════════════════════════════════════════════════

def _integers_from_text(text: str) -> list[int]:
    return [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", text) if n.replace(",", "").isdigit()]


def _param_from_href(href: str) -> str | None:
    """Return the first pagination-related query param found in a URL."""
    try:
        qs = parse_qs(urlparse(href).query)
        for key in qs:
            if key.lower() in (p.lower() for p in PAGINATION_PARAM_NAMES):
                return key
    except Exception:
        pass
    return None


def _path_segment_page(href: str) -> tuple:
    """
    Detect path-segment pagination and return (key, page_number).
    Handles:
      /page/2, /p/3, /pg/4           — standard absolute segments
      /page-2, /page-2.html          — hyphen style with leading slash
      page-2.html, page-3.html       — relative hrefs (no leading slash)
    Returns (None, None) if no match.
    """
    path = urlparse(href).path
    # Standard /page/N or /p/N (absolute)
    match = re.search(r"/(page|p|pg)/(\d+)", path, re.IGNORECASE)
    if match:
        return "path_segment", int(match.group(2))
    # Hyphen style with slash: /page-2 or /catalogue/page-2.html
    match = re.search(r"/(page|p|pg)-(\d+)(?:\.html?)?", path, re.IGNORECASE)
    if match:
        return "path_segment", int(match.group(2))
    # Relative href: page-2.html or page-2 (no leading slash)
    match = re.match(r"^(page|p|pg)-(\d+)(?:\.html?)?$", href.split("?")[0].split("/")[-1], re.IGNORECASE)
    if match:
        return "path_segment", int(match.group(2))
    return None, None


def parse_pagination(soup: BeautifulSoup, base_url: str) -> dict:
    """
    Analyse a BeautifulSoup tree for pagination.
    Returns dict: {found, type, key, max_page, notes}

    Max-page priority:
      P1  Explicit 'Last' button/link
      P2  Highest int in pagination container (with 10× sanity check)
      P3  'Page X of Y' text → Y
      P4  'Showing X-Y of Z' text → ceil(Z / page_size)
      P5  null + crawl note (prev/next only)
    """
    import math

    result = {
        "pagination_found": False,
        "pagination_type": "none",
        "pagination_key": None,
        "max_page": None,
        "notes": "",
    }

    keys_seen: list[str] = []
    has_js_trigger = False
    pag_container_hrefs: set[str] = set()

    # ── 0. Pre-scan pagination containers to mark their links ────────────
    for sel in PAGINATION_SELECTORS:
        try:
            for container in soup.select(sel):
                for ca in container.find_all("a", href=True):
                    pag_container_hrefs.add(ca["href"])
        except Exception:
            pass

    # ── 1. Scan <a> tags ──────────────────────────────────────────────────
    anchors = soup.find_all("a", href=True)
    for a in anchors:
        href = a["href"]
        in_container = href in pag_container_hrefs

        # Query-param pagination
        key = _param_from_href(href)
        if key:
            keys_seen.append(key)
            result["pagination_found"] = True

        # Path-segment pagination
        ps, ps_num = _path_segment_page(href)
        if ps and ps_num:
            if in_container or re.search(r"/(page|p|pg)[-/]", href, re.IGNORECASE):
                keys_seen.append(ps)
                result["pagination_found"] = True

    # ── 2. JS-trigger elements (data-page, data-value, onclick, href="#") ─
    js_triggers = soup.find_all(
        lambda tag: tag.has_attr("data-page")
        or tag.has_attr("data-href")
        or tag.has_attr("data-value")
        or (tag.has_attr("onclick") and "page" in tag["onclick"].lower())
        or (tag.has_attr("href") and tag["href"].startswith("#"))
    )
    if js_triggers:
        has_js_trigger = True
        result["pagination_found"] = True

    # ── 3. Pagination containers — extract keys and mark found ────────────
    for sel in PAGINATION_SELECTORS:
        try:
            for container in soup.select(sel):
                inner_anchors = container.find_all("a", href=True)
                for a in inner_anchors:
                    href = a["href"]
                    key = _param_from_href(href)
                    if not key:
                        ps_key, ps_num = _path_segment_page(href)
                        if ps_key:
                            key = ps_key
                    if key:
                        keys_seen.append(key)
                        result["pagination_found"] = True
                # Numbers in container text → signal pagination
                ctext = container.get_text()
                nums = [n for n in _integers_from_text(ctext) if n > 1]
                if nums:
                    result["pagination_found"] = True
        except Exception:
            pass

    # ── Determine pagination type and key ─────────────────────────────────
    if result["pagination_found"]:
        if keys_seen and not has_js_trigger:
            result["pagination_type"] = "link"
        elif has_js_trigger and not keys_seen:
            result["pagination_type"] = "js_redirect"
        elif has_js_trigger and keys_seen:
            result["pagination_type"] = "link"

    if keys_seen:
        result["pagination_key"] = max(set(keys_seen), key=keys_seen.count)
    elif result["pagination_found"] and has_js_trigger:
        result["pagination_key"] = "js_trigger"

    # ══════════════════════════════════════════════════════════════════════
    # MAX PAGE — 5-priority logic (returns on first match)
    # ══════════════════════════════════════════════════════════════════════

    pag_type = result["pagination_type"]

    # Types with no meaningful page count
    if pag_type in ("api", "none"):
        result["max_page"] = None
        return result

    full_text = soup.get_text(" ", strip=True)

    # ── Priority 1: Explicit "Last" link with a readable page number ──────
    for a in anchors:
        text_lower = a.get_text(strip=True).lower()
        if text_lower in ("last", "last »", "»", "last page", "›", ">>"):
            href = a["href"]
            key = _param_from_href(href)
            if key:
                qs = parse_qs(urlparse(href).query)
                nums = _integers_from_text(" ".join(qs.get(key, [])))
                if nums:
                    result["max_page"] = max(nums)
                    return result
            ps_key, ps_num = _path_segment_page(href)
            if ps_key and ps_num:
                result["max_page"] = ps_num
                return result

    # ── Priority 2: Highest integer in pagination container ───────────────
    #
    # Collect numbers ONLY from elements with purely-numeric text content
    # (page-number badges like <a>3</a>, <span>114</span>) inside known
    # pagination containers.  This avoids pulling in result-count prose.
    #
    # Sanity check: if the highest candidate is > 10× the largest number
    # found in an actual <a> href (i.e. the highest *linked* page), mark it
    # as a result-count outlier and discard.  This handles "1 2 3 · · · 5,432
    # items" while keeping Amazon-style "1 2 3 … 114" intact (114 is not
    # reachable via a direct href, but 10×3=30 < 114 would normally trip the
    # rule — so we look at href-based max first).
    container_page_nums: list[int] = []   # from pure-numeric elements
    href_page_nums: list[int] = []         # from actual <a href= page links

    for sel in PAGINATION_SELECTORS:
        try:
            for container in soup.select(sel):
                # Purely-numeric child elements  →  page-number badges
                for el in container.find_all(["a", "span", "li", "button"]):
                    txt = el.get_text(strip=True)
                    if txt.isdigit() and 1 < int(txt) <= 9999:
                        container_page_nums.append(int(txt))
                # Numbers embedded in href ?page= or path /page/N
                for a in container.find_all("a", href=True):
                    key = _param_from_href(a["href"])
                    if key:
                        qs = parse_qs(urlparse(a["href"]).query)
                        href_page_nums.extend(_integers_from_text(" ".join(qs.get(key, []))))
                    _, ps_num = _path_segment_page(a["href"])
                    if ps_num:
                        href_page_nums.append(ps_num)
        except Exception:
            pass

    if container_page_nums:
        highest = max(container_page_nums)
        max_linked = max(href_page_nums) if href_page_nums else highest
        # Only discard if highest is wildly above both linked pages AND > 9999
        if highest > 10 * max_linked and highest > 9999:
            pass   # looks like a result count — fall through
        elif highest > 1:
            result["max_page"] = highest
            return result

    # ── Priority 3: "Page X of Y" or "X of Y pages" ──────────────────────
    m = re.search(r"[Pp]age\s+\d+\s+of\s+(\d+)", full_text)
    if not m:
        m = re.search(r"\d+\s+of\s+(\d+)\s+pages?", full_text)
    if m:
        val = int(m.group(1).replace(",", ""))
        if 1 < val <= 9999:
            result["max_page"] = val
            return result

    # ── Priority 4: "Showing X–Y of Z" → ceil(Z / page_size) ────────────
    m = re.search(
        r"[Ss]howing\s+([\d,]+)\s*[-–]\s*([\d,]+)\s+of\s+([\d,]+)",
        full_text,
    )
    if m:
        try:
            x = int(m.group(1).replace(",", ""))
            y = int(m.group(2).replace(",", ""))
            z = int(m.group(3).replace(",", ""))
            page_size = y - x + 1
            if page_size > 0 and z > 0:
                result["max_page"] = math.ceil(z / page_size)
                return result
        except Exception:
            pass

    # ── Priority 5: prev/next only — max_page undetermined ───────────────
    result["max_page"] = None
    if pag_type in ("link", "js_redirect"):
        note = "max_page undetermined — only prev/next links found; forward crawl required."
        result["notes"] = (result["notes"] + " | " + note).strip(" | ")

    return result






# ═══════════════════════════════════════════════════════════════════════════
# REQUESTS-BASED FETCH
# ═══════════════════════════════════════════════════════════════════════════

def fetch_with_requests(url: str) -> dict:
    """Fetch URL using requests. Returns partial result dict."""
    try:
        session = requests.Session()
        resp = session.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        body = resp.text
        resp_headers = dict(resp.headers)

        blocked, block_type = detect_block(resp.status_code, resp_headers, body)
        if blocked:
            return {
                "status_code": resp.status_code,
                "blocked": True,
                "block_type": block_type,
                "body": None,
                "fetch_method": "requests",
            }

        return {
            "status_code": resp.status_code,
            "blocked": False,
            "block_type": None,
            "body": body,
            "fetch_method": "requests",
            # Only trigger Playwright if the body is too short to contain real content
            # (threshold: 2000 chars — avoids false positives on large plain-HTML pages)
            "needs_playwright": is_js_rendered(body) or len(resp.text.strip()) < 2000,
        }
    except Exception as e:
        return {
            "status_code": None,
            "blocked": False,
            "block_type": None,
            "body": None,
            "fetch_method": "requests",
            "error": str(e),
        }


# ═══════════════════════════════════════════════════════════════════════════
# PLAYWRIGHT-BASED FETCH (with XHR interception)
# ═══════════════════════════════════════════════════════════════════════════

async def fetch_with_playwright(url: str, intercept: bool = False) -> dict:
    """Fetch URL using Playwright with a hard 25-second overall timeout."""
    if not PLAYWRIGHT_AVAILABLE:
        return {
            "body": None,
            "fetch_method": "playwright",
            "error": "Playwright not installed. Run: pip install playwright && playwright install chromium",
            "api_pagination": None,
        }

    async def _do_fetch() -> dict:
        intercepted_urls: list[str] = []
        browser = None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    extra_http_headers={
                        k: v for k, v in HEADERS.items() if k != "User-Agent"
                    },
                )
                page = await context.new_page()

                if intercept:
                    async def on_request(request):
                        req_url = request.url
                        for param in PAGINATION_PARAM_NAMES:
                            if param in parse_qs(urlparse(req_url).query):
                                intercepted_urls.append(req_url)
                                break
                    page.on("request", on_request)

                body = None
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=12_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=4_000)
                    except Exception:
                        pass
                    body = await page.content()
                except Exception:
                    body = None

                await browser.close()
        except Exception as e:
            return {"body": None, "fetch_method": "playwright", "error": str(e),
                    "api_pagination": None, "intercepted_urls": []}

        # Parse API pagination from intercepted URLs
        api_pagination = None
        if intercept and intercepted_urls:
            best = intercepted_urls[0]
            qs = parse_qs(urlparse(best).query)
            for param in PAGINATION_PARAM_NAMES:
                if param in qs:
                    api_pagination = {"url": best, "pagination_key": param}
                    break

        return {
            "body": body,
            "fetch_method": "playwright_intercept" if intercept else "playwright",
            "api_pagination": api_pagination,
            "intercepted_urls": intercepted_urls if intercept else [],
        }

    try:
        return await asyncio.wait_for(_do_fetch(), timeout=25.0)
    except asyncio.TimeoutError:
        return {
            "body": None,
            "fetch_method": "playwright_intercept" if intercept else "playwright",
            "error": "Playwright hard timeout (25s) exceeded.",
            "api_pagination": None,
            "intercepted_urls": [],
        }


# ═══════════════════════════════════════════════════════════════════════════
# API PAGINATION RESULT BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_api_pagination_result(url: str, api_info: dict, fetch_method: str) -> dict:
    """Build result for API-intercepted pagination."""
    api_url = api_info.get("url", "")
    key = api_info.get("pagination_key")
    qs = parse_qs(urlparse(api_url).query)

    # Try to find max page from API URL params (e.g. total or limit)
    max_page = None
    limit = None
    for lk in ("limit", "per_page", "pageSize", "page_size"):
        if lk in qs:
            try:
                limit = int(qs[lk][0])
            except Exception:
                pass

    return {
        "url": url,
        "block_detected": False,
        "block_type": None,
        "fetch_method": fetch_method,
        "pagination_found": True,
        "pagination_type": "api",
        "pagination_key": key,
        "max_page": max_page,
        "notes": f"Pagination detected via intercepted API call: {api_url}",
    }


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ANALYSE FUNCTION
# ═══════════════════════════════════════════════════════════════════════════

async def analyse_url(url: str, no_playwright: bool = False) -> dict:
    """Full pipeline for a single URL."""
    base_result = {
        "url": url,
        "block_detected": False,
        "block_type": None,
        "fetch_method": "requests",
        "pagination_found": False,
        "pagination_type": "none",
        "pagination_key": None,
        "max_page": None,
        "notes": "",
    }

    # ── Step 1: Try requests ──────────────────────────────────────────────
    req_result = fetch_with_requests(url)

    if req_result.get("error"):
        base_result["notes"] = f"requests error: {req_result['error']}"
        if no_playwright:
            return base_result
        # Try playwright as fallback
        pw = await fetch_with_playwright(url, intercept=True)
        if pw.get("error"):
            base_result["notes"] += f" | playwright error: {pw['error']}"
            return base_result
        req_result = {**req_result, **pw, "needs_playwright": False}
        if pw.get("api_pagination"):
            return build_api_pagination_result(url, pw["api_pagination"], pw["fetch_method"])

    # ── Step 2: Hard block? ───────────────────────────────────────────────
    if req_result.get("blocked"):
        base_result["block_detected"] = True
        base_result["block_type"] = req_result["block_type"]
        base_result["fetch_method"] = "requests"
        base_result["notes"] = (
            f"HTTP {req_result.get('status_code')} — {req_result['block_type']} detected."
        )
        return base_result

    body = req_result.get("body") or ""
    fetch_method = req_result.get("fetch_method", "requests")

    # ── Step 3: Needs Playwright? ─────────────────────────────────────────
    if req_result.get("needs_playwright") and not no_playwright:
        notes_extra = "JS framework detected — fell back to Playwright."
        pw = await fetch_with_playwright(url, intercept=True)
        if pw.get("error"):
            base_result["notes"] = notes_extra + f" Playwright error: {pw['error']}"
        else:
            body = pw.get("body") or body
            fetch_method = pw.get("fetch_method", "playwright")
            if pw.get("api_pagination"):
                return build_api_pagination_result(url, pw["api_pagination"], fetch_method)
            base_result["notes"] = notes_extra

    # ── Step 4: Parse pagination from HTML ───────────────────────────────
    if body:
        soup = BeautifulSoup(body, "html.parser")
        pag = parse_pagination(soup, url)
        base_result.update(pag)
        base_result["fetch_method"] = fetch_method

        # If still nothing found, only try Playwright if body was small
        # (large bodies that found no pagination genuinely have none)
        if (not pag["pagination_found"] and fetch_method == "requests"
                and PLAYWRIGHT_AVAILABLE and not no_playwright):
            if len(body) < 50_000:  # skip Playwright for large pages already parsed
                pw = await fetch_with_playwright(url, intercept=True)
                if pw.get("api_pagination"):
                    return build_api_pagination_result(url, pw["api_pagination"], pw["fetch_method"])
                if pw.get("body"):
                    soup2 = BeautifulSoup(pw["body"], "html.parser")
                    pag2 = parse_pagination(soup2, url)
                    if pag2["pagination_found"]:
                        base_result.update(pag2)
                        base_result["fetch_method"] = pw["fetch_method"]
    else:
        base_result["notes"] = "Empty body received."

    return base_result


# ═══════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(
        description="Detect pagination on MRP URLs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("urls", nargs="*", help="One or more URLs to analyse")
    parser.add_argument(
        "--file", "-f",
        help="Path to a text file with one URL per line",
    )
    parser.add_argument(
        "--output", "-o",
        help="Write JSON results to this file (default: stdout)",
    )
    parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print JSON output",
    )
    parser.add_argument(
        "--no-playwright", dest="no_playwright", action="store_true",
        help="Disable Playwright fallback entirely (requests-only mode)",
    )
    args = parser.parse_args()

    urls = list(args.urls)
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            urls += [line.strip() for line in fh if line.strip() and not line.startswith("#")]

    if not urls:
        parser.print_help()
        sys.exit(1)

    results = []
    for url in urls:
        print(f"  Analysing: {url}", file=sys.stderr)
        result = await analyse_url(url, no_playwright=args.no_playwright)
        results.append(result)
        # Streaming print to stderr so user can see progress
        print(f"  → {result['pagination_type']} | key={result['pagination_key']} | max={result['max_page']} | blocked={result['block_detected']}", file=sys.stderr)

    indent = 2 if args.pretty else None
    json_output = json.dumps(results if len(results) > 1 else results[0], indent=indent)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(json_output)
        print(f"\nResults written to {args.output}", file=sys.stderr)
    else:
        print(json_output)


if __name__ == "__main__":
    asyncio.run(main())
