"""
pagination_detector.py
======================
Detects pagination on any MRP (Multiple Record Page) URL.

Changelog (latest fixes):
  - Newegg Monitors null max_page: added X/Y slash pattern (e.g. "1/15") + data-page attr scan
  - Etsy pagination not found: added deep bottom-scroll retry + total-results count estimation
  - Newegg pagination_key "path_segment" → actual keyword e.g. "page"
  - Newegg load-more false positive (FAQ buttons) → excluded by keyword filter
  - Amazon IntersectionObserver false infinite-scroll → weak-signal guard added

Install:
  pip install playwright playwright-stealth requests beautifulsoup4 curl_cffi 2captcha-python
  playwright install chromium

CAPTCHA setup:
  export CAPTCHA_API_KEY="your_2captcha_key"   OR   --captcha-key YOUR_KEY

Usage:
  python pagination_detector.py <url>
  python pagination_detector.py --file urls.txt --pretty
  python pagination_detector.py <url> --debug
  python pagination_detector.py <url> --captcha-key KEY
  python pagination_detector.py <url> --proxy http://user:pass@host:port
"""

import argparse
import asyncio
import json
import math
import os
import random
import re
import sys
import time
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

# curl_cffi: Chrome TLS fingerprint impersonation
try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    import requests as curl_requests
    CURL_CFFI_AVAILABLE = False

import requests as std_requests

# Playwright
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# playwright-stealth
try:
    from playwright_stealth import stealth_async
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

# 2captcha
try:
    from twocaptcha import TwoCaptcha
    CAPTCHA_SOLVER_AVAILABLE = True
except ImportError:
    CAPTCHA_SOLVER_AVAILABLE = False


# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

STEALTH_CONTEXT = {
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "viewport": {"width": 1366, "height": 768},
}

CLOUDFLARE_SIGNALS = [
    "just a moment", "checking your browser", "cf-ray",
    "cloudflare", "__cf_bm", "ray id",
]

PERIMETERX_SIGNALS = [
    "px-captcha", "px-cloud.net", "captcha.px-cloud",
    "access to this page has been denied",
    "_pxAppId", "pxcaptcha", "px-captcha-background",
]

BLOCK_BODY_SIGNALS = (
    CLOUDFLARE_SIGNALS + PERIMETERX_SIGNALS
    + ["access denied", "403 forbidden",
       "enable javascript and cookies to continue"]
)

JS_FRAMEWORK_MARKERS = [
    "__NEXT_DATA__", "ng-app", "data-reactroot", "__vue__",
    "data-react-helmet", "nuxt", "_app.js",
]

PAGINATION_PARAM_NAMES = [
    "page", "p", "pg", "paged", "pagenum", "pageno",
    "start", "offset", "from", "skip",
    "limit", "per_page", "pageSize", "page_size",
    "currentPage", "current_page",
]

PAGINATION_SELECTORS = [
    # Standard
    ".pagination", ".pager", ".paginator",
    "[aria-label*='pagination']", "[aria-label*='Pagination']",
    "[aria-label*='Page navigation']", "[aria-label*='page navigation']",
    "nav ul", ".page-numbers", ".pages",
    # Class fragments
    "[class*='paginat']", "[class*='pageNav']",
    "[class*='page-nav']", "[class*='Pagination']",
    "[id*='pagination']",
    # Data attributes — Etsy, React/Vue apps
    "[data-testid*='pagination']", "[data-testid*='paging']",
    "[data-wt-pagination]", "[data-pagination]",
    "[data-page-number]",
    # Newegg / e-commerce specific
    "[class*='list-tool-page']", "[class*='tool-page']",
    # Scroll / load-more containers (scanned separately but included for href scan)
    ".infinite-scroll-component", "[data-infinite-scroll]",
    ".load-more", "[class*='loadMore']", "[class*='load-more']",
    "button[data-page]", "[class*='ShowMore']",
]

LOAD_MORE_EXCLUSION_KEYWORDS = [
    "faq", "review", "question", "answer", "comment",
    "description", "spec", "detail", "article", "blog",
    "guide", "help", "support",
]

LOAD_MORE_SELECTORS = [
    ".load-more", "[class*='loadMore']", "[class*='load-more']",
    "button[class*='more']", "[data-testid*='load-more']",
    "[class*='ShowMore']", "[class*='show-more']",
    "button[class*='More']",
]

NON_PRODUCT_CONTAINERS = [
    ".faq", "[class*='faq']", "[id*='faq']",
    ".review", "[class*='review']",
    ".qa", "[class*='questions']",
    "footer", ".footer",
    ".sidebar", "[class*='sidebar']",
]

INFINITE_SCROLL_STRONG_SIGNALS = {
    "infinite-scroll", "data-infinite", "infiniteScroll",
    "infinite_scroll", "react-infinite",
}

INFINITE_SCROLL_WEAK_SIGNALS = {
    "IntersectionObserver", "loadMore",
}

POST_SCROLL_WAIT_SELECTORS = [
    ".pagination", "[aria-label*='pagination']",
    "[data-testid*='pagination']", "[data-wt-pagination]",
    ".pager", "[class*='paginat']",
    ".load-more", "[class*='loadMore']",
    "nav ul li a",
]

# Etsy/SPA default items per page for total-count estimation
DEFAULT_ITEMS_PER_PAGE = 48

PX_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const d = ctx.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < 10; i++) d.data[i*4] ^= Math.floor(Math.random()*3);
            ctx.putImageData(d, 0, 0);
        }
        return origToDataURL.call(this, type);
    };
    window._mouseHistory = [];
    for (let i = 0; i < 10; i++) {
        window._mouseHistory.push({
            x: Math.floor(Math.random()*1366),
            y: Math.floor(Math.random()*768),
            t: Date.now() - (10-i)*200
        });
    }
    document.addEventListener('mousemove', function(e) {
        window._mouseHistory.push({x:e.clientX, y:e.clientY, t:Date.now()});
        if (window._mouseHistory.length > 50) window._mouseHistory.shift();
    }, true);
"""


# ---------------------------------------------------------------------------
# BLOCK AND CAPTCHA DETECTION
# ---------------------------------------------------------------------------

def detect_block(status_code, headers, body):
    body_lower = (body or "").lower()
    headers_str = str(headers).lower()
    if status_code in (403, 429, 503):
        if any(s in body_lower or s in headers_str for s in CLOUDFLARE_SIGNALS):
            return True, "cloudflare"
        if any(s in body_lower for s in PERIMETERX_SIGNALS):
            return True, "perimeterx"
        return True, "hard_block"
    #if headers.get("cf-ray") or headers.get("CF-RAY"):
    #    return True, "cloudflare"
    if any(s in body_lower for s in PERIMETERX_SIGNALS):
        return True, "perimeterx"
    if "just a moment" in body_lower or "checking your browser" in body_lower:
        return True, "cloudflare"
    return False, None


def detect_captcha(body):
    if not body:
        return False, None, None
    b = body.lower()
    if "px-cloud.net" in b or "px-captcha" in b:
        m = re.search(r"captcha\.px-cloud\.net/([A-Za-z0-9]+)/captcha\.js", body)
        return True, "perimeterx", (m.group(1) if m else None)
    if "cf-turnstile" in b or "challenges.cloudflare.com/turnstile" in b:
        m = re.search(r'data-sitekey=["\']([^"\']+)["\']', body)
        return True, "turnstile", (m.group(1) if m else None)
    if "hcaptcha.com" in b or "h-captcha" in b:
        m = re.search(r'data-sitekey=["\']([^"\']+)["\']', body)
        return True, "hcaptcha", (m.group(1) if m else None)
    if "recaptcha" in b or "g-recaptcha" in b:
        m = re.search(r'data-sitekey=["\']([^"\']+)["\']', body)
        return True, "recaptcha", (m.group(1) if m else None)
    return False, None, None


def _is_still_blocked(body):
    if not body:
        return True
    return any(sig in body.lower() for sig in BLOCK_BODY_SIGNALS)


def is_js_rendered(body):
    return any(m in body for m in JS_FRAMEWORK_MARKERS)


# ---------------------------------------------------------------------------
# CAPTCHA SOLVER
# ---------------------------------------------------------------------------

async def solve_captcha_on_page(page, url, captcha_type, sitekey, api_key):
    if not CAPTCHA_SOLVER_AVAILABLE:
        print("  [!] pip install 2captcha-python", file=sys.stderr)
        return False
    if not api_key or not sitekey:
        print("  [CAPTCHA] No API key or sitekey — skipping.", file=sys.stderr)
        return False

    print(f"  [CAPTCHA] Solving {captcha_type} ({sitekey[:20]}...) via 2captcha...", file=sys.stderr)
    try:
        solver = TwoCaptcha(api_key)
        if captcha_type == "perimeterx":
            result = solver.perimeterx(app_id=sitekey, url=url)
        elif captcha_type == "recaptcha":
            result = solver.recaptcha(sitekey=sitekey, url=url)
        elif captcha_type == "hcaptcha":
            result = solver.hcaptcha(sitekey=sitekey, url=url)
        elif captcha_type == "turnstile":
            result = solver.turnstile(sitekey=sitekey, url=url)
        else:
            return False

        token = result.get("code")
        if not token:
            print("  [CAPTCHA] No token returned.", file=sys.stderr)
            return False

        print("  [CAPTCHA] Injecting token...", file=sys.stderr)
        if captcha_type == "perimeterx":
            await page.evaluate(f"""
                window._pxParam1 = '{token}';
                try {{ document.querySelector('input[name="_pxCaptcha"]').value = '{token}'; }} catch(e) {{}}
                try {{ var f = document.querySelector('form'); if(f) f.submit(); }} catch(e) {{}}
            """)
        elif captcha_type == "recaptcha":
            await page.evaluate(f"""
                try {{ document.getElementById('g-recaptcha-response').innerHTML = '{token}'; }} catch(e) {{}}
                try {{
                    Object.entries(___grecaptcha_cfg.clients).forEach(([k,v]) => {{
                        if(v.callback) v.callback('{token}');
                    }});
                }} catch(e) {{}}
            """)
        elif captcha_type == "hcaptcha":
            await page.evaluate(f"""
                try {{ document.querySelector('[name="h-captcha-response"]').value = '{token}'; }} catch(e) {{}}
                try {{ if(typeof hcaptcha!=='undefined') hcaptcha.execute(); }} catch(e) {{}}
            """)
        elif captcha_type == "turnstile":
            await page.evaluate(f"""
                try {{ document.querySelector('[name="cf-turnstile-response"]').value = '{token}'; }} catch(e) {{}}
                try {{ if(typeof turnstile!=='undefined') turnstile.implicitRender(); }} catch(e) {{}}
            """)

        await asyncio.sleep(2)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        print("  [CAPTCHA] Done.", file=sys.stderr)
        return True

    except Exception as e:
        print(f"  [CAPTCHA] Error: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# PAGINATION PARSING HELPERS
# ---------------------------------------------------------------------------

def _ints(text):
    return [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", text)
            if n.replace(",", "").isdigit()]


def _param_from_href(href):
    try:
        qs = parse_qs(urlparse(href).query)
        for key in qs:
            if key.lower() in (p.lower() for p in PAGINATION_PARAM_NAMES):
                return key
    except Exception:
        pass
    return None


def _path_page(href):
    """
    Detect path-segment pagination.
    Returns (keyword, page_number) e.g. ("page", 2) — NOT the generic "path_segment".
    """
    path = urlparse(href).path
    for pat in [r"/(page|p|pg)/(\d+)", r"/(page|p|pg)-(\d+)(?:\.html?)?"]:
        m = re.search(pat, path, re.I)
        if m:
            return m.group(1).lower(), int(m.group(2))
    m = re.match(r"^(page|p|pg)-(\d+)(?:\.html?)?$",
                 href.split("?")[0].split("/")[-1], re.I)
    if m:
        return m.group(1).lower(), int(m.group(2))
    return None, None


def _detect_infinite_scroll(soup, raw):
    for sel in ["[data-infinite-scroll]", "[infinite-scroll]",
                ".infinite-scroll-component", "[data-testid='infinite-scroll']"]:
        if soup.select(sel):
            return True
    strong_found = any(sig in raw for sig in INFINITE_SCROLL_STRONG_SIGNALS)
    weak_found = any(sig in raw for sig in INFINITE_SCROLL_WEAK_SIGNALS)
    if strong_found:
        return True
    if weak_found:
        for sel in PAGINATION_SELECTORS:
            try:
                if soup.select(sel):
                    return False
            except Exception:
                pass
        for a in soup.find_all("a", href=True):
            if _param_from_href(a["href"]) or _path_page(a["href"])[0]:
                return False
        return True
    return False


def _element_in_non_product_container(el, soup):
    for sel in NON_PRODUCT_CONTAINERS:
        try:
            for container in soup.select(sel):
                if el in container.descendants:
                    return True
        except Exception:
            pass
    return False


def _detect_load_more(soup):
    for sel in LOAD_MORE_SELECTORS:
        try:
            for el in soup.select(sel):
                text = el.get_text(strip=True)
                if any(kw in text.lower() for kw in LOAD_MORE_EXCLUSION_KEYWORDS):
                    continue
                if _element_in_non_product_container(el, soup):
                    continue
                return {"found": True, "text": text,
                        "data_page": el.get("data-page"), "onclick": el.get("onclick")}
        except Exception:
            continue
    return {"found": False}


# ---------------------------------------------------------------------------
# [FIX] MAX PAGE HELPERS
# ---------------------------------------------------------------------------

def _max_page_from_data_attrs(soup):
    """
    Extract max page from data-page attributes anywhere on the page.
    Catches Newegg-style JS pagination where links use href="#" data-page="N".
    """
    nums = []
    for el in soup.find_all(attrs={"data-page": True}):
        try:
            v = int(el["data-page"])
            if 1 < v <= 9999:
                nums.append(v)
        except (ValueError, TypeError):
            pass
    return max(nums) if nums else None


def _max_page_from_slash_pattern(text):
    """
    Extract max page from 'X/Y' patterns like '1/15' or 'Page 1/20'.
    Used by Newegg and many e-commerce sites that show current/total pages.
    """
    m = re.search(r'\b(\d+)\s*/\s*(\d+)\b', text)
    if m:
        total = int(m.group(2))
        if 1 < total <= 9999:
            return total
    return None


def _max_page_from_total_count(soup):
    """
    Estimate max_page from total result count text.
    Handles:
      '1-48 of 50,000 results'    -> ceil(50000 / 48) = 1042
      '50,000+ results'           -> ceil(50000 / 48) = 1042  (assumes 48/page)
      'About 48,000 results'      -> ceil(48000 / 48) = 1000
    Used as a fallback when no explicit pagination links are found (e.g. Etsy).
    """
    text = soup.get_text(" ", strip=True)

    # Pattern 1: "X-Y of Z results" — per_page is derived from range
    m = re.search(
        r'(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)\s+of\s+([\d,]+)\+?\s+results?',
        text, re.I
    )
    if m:
        try:
            x = int(m.group(1).replace(",", ""))
            y = int(m.group(2).replace(",", ""))
            z = int(m.group(3).replace(",", ""))
            per_page = y - x + 1
            if per_page > 0 and z > 0:
                return math.ceil(z / per_page), per_page, z
        except Exception:
            pass

    # Pattern 2: standalone "N results" or "N+ results" — assume default per_page
    m = re.search(r'\b([\d,]+)\+?\s+results?\b', text, re.I)
    if m:
        try:
            z = int(m.group(1).replace(",", ""))
            if z > 0:
                return math.ceil(z / DEFAULT_ITEMS_PER_PAGE), DEFAULT_ITEMS_PER_PAGE, z
        except Exception:
            pass

    return None, None, None


# ---------------------------------------------------------------------------
# MAIN PAGINATION PARSER
# ---------------------------------------------------------------------------

def parse_pagination(soup, base_url, raw_html=""):
    r = {
        "pagination_found": False,
        "pagination_type": "none",
        "pagination_key": None,
        "max_page": None,
        "notes": "",
    }

    # ── Infinite scroll ───────────────────────────────────────────────────
    if _detect_infinite_scroll(soup, raw_html):
        max_p, per_p, total = _max_page_from_total_count(soup)
        r.update({
            "pagination_found": True,
            "pagination_type": "infinite_scroll",
            "max_page": max_p,
            "notes": (
                f"Infinite scroll — estimated {max_p} pages "
                f"({total} results / {per_p} per page)"
                if max_p else "Infinite scroll — max_page undeterminable"
            ),
        })
        return r

    # ── Load More (product-only after exclusion) ──────────────────────────
    lm = _detect_load_more(soup)
    if lm["found"]:
        max_p, per_p, total = _max_page_from_total_count(soup)
        r.update({
            "pagination_found": True,
            "pagination_type": "load_more",
            "pagination_key": "js_trigger",
            "max_page": max_p,
            "notes": (
                f"Load More button: '{lm['text']}'"
                + (f" — estimated {max_p} pages ({total} results / {per_p} per page)"
                   if max_p else "")
            ),
        })
        return r

    # ── Link / JS pagination ──────────────────────────────────────────────
    keys_seen, has_js = [], False
    container_hrefs = set()

    for sel in PAGINATION_SELECTORS:
        try:
            for c in soup.select(sel):
                for a in c.find_all("a", href=True):
                    container_hrefs.add(a["href"])
        except Exception:
            pass

    anchors = soup.find_all("a", href=True)

    for a in anchors:
        href = a["href"]
        in_c = href in container_hrefs
        k = _param_from_href(href)
        if k:
            keys_seen.append(k)
            r["pagination_found"] = True
        ps_key, ps_num = _path_page(href)
        if ps_key and ps_num and (in_c or re.search(r"/(page|p|pg)[-/]", href, re.I)):
            keys_seen.append(ps_key)
            r["pagination_found"] = True

    js_els = soup.find_all(lambda t:
        t.has_attr("data-page") or t.has_attr("data-href") or t.has_attr("data-value")
        or (t.has_attr("onclick") and "page" in t["onclick"].lower())
        or (t.has_attr("href") and t["href"].startswith("#"))
    )
    if js_els:
        has_js = True
        r["pagination_found"] = True

    for sel in PAGINATION_SELECTORS:
        try:
            for c in soup.select(sel):
                for a in c.find_all("a", href=True):
                    k = _param_from_href(a["href"])
                    if not k:
                        pk, _ = _path_page(a["href"])
                        if pk:
                            k = pk
                    if k:
                        keys_seen.append(k)
                        r["pagination_found"] = True
                if [n for n in _ints(c.get_text()) if n > 1]:
                    r["pagination_found"] = True
        except Exception:
            pass

    # Broad fallback scan — catches Etsy and sites with pagination
    # links outside standard container selectors
    if not r["pagination_found"]:
        for a in anchors:
            href = a["href"]
            k = _param_from_href(href)
            if k:
                try:
                    val = parse_qs(urlparse(href).query).get(k, [None])[0]
                    if val and int(val) >= 2:
                        keys_seen.append(k)
                        r["pagination_found"] = True
                except Exception:
                    pass
            ps_key, ps_num = _path_page(href)
            if ps_key and ps_num and ps_num >= 2:
                keys_seen.append(ps_key)
                r["pagination_found"] = True
        if r["pagination_found"]:
            r["notes"] = "Detected via broad fallback anchor scan"

    # Classify type and key
    if r["pagination_found"]:
        if keys_seen and not has_js:
            r["pagination_type"] = "link"
        elif has_js and not keys_seen:
            r["pagination_type"] = "js_redirect"
        else:
            r["pagination_type"] = "link"

    if keys_seen:
        r["pagination_key"] = max(set(keys_seen), key=keys_seen.count)
    elif r["pagination_found"] and has_js:
        r["pagination_key"] = "js_trigger"

    # ── Max page — 6-priority logic ───────────────────────────────────────
    pt = r["pagination_type"]
    if pt in ("api", "none"):
        # Even if no pagination found, try to estimate from total count
        # (useful for Etsy where pagination links don't render in headless)
        max_p, per_p, total = _max_page_from_total_count(soup)
        if max_p:
            r.update({
                "pagination_found": True,
                "pagination_type": "estimated",
                "pagination_key": "page",
                "max_page": max_p,
                "notes": (
                    f"No pagination links found — estimated {max_p} pages "
                    f"from total count ({total} results / {per_p} per page)"
                ),
            })
        return r

    ft = soup.get_text(" ", strip=True)

    # P1: Explicit Last link
    for a in anchors:
        tl = a.get_text(strip=True).lower()
        if tl in ("last", "last »", "»", "last page", "›", ">>"):
            href = a["href"]
            k = _param_from_href(href)
            if k:
                ns = _ints(" ".join(parse_qs(urlparse(href).query).get(k, [])))
                if ns:
                    r["max_page"] = max(ns)
                    return r
            pk, pn = _path_page(href)
            if pk and pn:
                r["max_page"] = pn
                return r

    # P2: Highest integer in pagination container
    cpn, hpn = [], []
    for sel in PAGINATION_SELECTORS:
        try:
            for c in soup.select(sel):
                for el in c.find_all(["a", "span", "li", "button"]):
                    t = el.get_text(strip=True)
                    if t.isdigit() and 1 < int(t) <= 9999:
                        cpn.append(int(t))
                for a in c.find_all("a", href=True):
                    k = _param_from_href(a["href"])
                    if k:
                        hpn.extend(_ints(" ".join(
                            parse_qs(urlparse(a["href"]).query).get(k, [])
                        )))
                    _, pn = _path_page(a["href"])
                    if pn:
                        hpn.append(pn)
        except Exception:
            pass

    # Also collect from broad fallback anchors if container scan found nothing
    if not cpn:
        for a in anchors:
            k = _param_from_href(a["href"])
            if k:
                try:
                    vals = _ints(" ".join(parse_qs(urlparse(a["href"]).query).get(k, [])))
                    cpn.extend(vals)
                    hpn.extend(vals)
                except Exception:
                    pass
            _, pn = _path_page(a["href"])
            if pn:
                cpn.append(pn)
                hpn.append(pn)

    if cpn:
        hi = max(cpn)
        ml = max(hpn) if hpn else hi
        if not (hi > 10 * ml and hi > 9999) and hi > 1:
            r["max_page"] = hi
            return r

    # P3: "Page X of Y" or "X of Y pages"
    m = re.search(r"[Pp]age\s+\d+\s+of\s+(\d+)", ft)
    if not m:
        m = re.search(r"\d+\s+of\s+(\d+)\s+pages?", ft)
    if m:
        v = int(m.group(1).replace(",", ""))
        if 1 < v <= 9999:
            r["max_page"] = v
            return r

    # P3b: [FIX] "X/Y" slash format — e.g. Newegg "1/15"
    slash_max = _max_page_from_slash_pattern(ft)
    if slash_max:
        r["max_page"] = slash_max
        return r

    # P4: "Showing X–Y of Z results"
    m = re.search(r"[Ss]howing\s+([\d,]+)\s*[-–]\s*([\d,]+)\s+of\s+([\d,]+)", ft)
    if m:
        try:
            x, y, z = (int(m.group(i).replace(",", "")) for i in (1, 2, 3))
            ps = y - x + 1
            if ps > 0 and z > 0:
                r["max_page"] = math.ceil(z / ps)
                return r
        except Exception:
            pass

    # P4b: [FIX] data-page attribute scan — covers JS-paginated sites
    # where links use href="#" + data-page="N" (Newegg Monitors style)
    dp_max = _max_page_from_data_attrs(soup)
    if dp_max:
        r["max_page"] = dp_max
        return r

    # P5: Total count estimation — fallback when no numbered links visible
    max_p, per_p, total = _max_page_from_total_count(soup)
    if max_p:
        r["max_page"] = max_p
        r["notes"] = (
            (r["notes"] + " | " if r["notes"] else "")
            + f"max_page estimated from total count ({total} results / {per_p} per page)"
        )
        return r

    # P6: prev/next only
    if pt in ("link", "js_redirect"):
        r["notes"] = (
            r["notes"] + " | max_page undetermined — forward crawl required."
        ).strip(" | ")

    return r


# ---------------------------------------------------------------------------
# REQUESTS FETCH
# ---------------------------------------------------------------------------

def fetch_with_requests(url):
    try:
        if CURL_CFFI_AVAILABLE:
            resp = curl_requests.get(url, headers=HEADERS, timeout=20, impersonate="chrome124")
        else:
            s = std_requests.Session()
            resp = s.get(url, headers=HEADERS, timeout=20, allow_redirects=True)

        body = resp.text
        h = dict(resp.headers)
        blocked, btype = detect_block(resp.status_code, h, body)
        if blocked:
            return {"status_code": resp.status_code, "blocked": True,
                    "block_type": btype, "body": None, "fetch_method": "requests"}
        return {
            "status_code": resp.status_code, "blocked": False, "block_type": None,
            "body": body, "fetch_method": "requests",
            "needs_playwright": (
                is_js_rendered(body)
                or len(resp.text.strip()) < 2000
                or (len(resp.text.strip()) < 100_000 and not any(
                    sig in body for sig in [
                        "data-component-type", "s-result-item",
                        "class=\"product", "class='product",
                        "data-testid=\"product",
                    ]
                ))
            ),
        }
    except Exception as e:
        return {"status_code": None, "blocked": False, "block_type": None,
                "body": None, "fetch_method": "requests", "error": str(e)}


# ---------------------------------------------------------------------------
# DEEP INTERACTION FETCH
# ---------------------------------------------------------------------------

async def fetch_with_deep_interaction(url, captcha_api_key=None, proxy=None, debug=False):
    if not PLAYWRIGHT_AVAILABLE:
        return {"body": None, "fetch_method": "playwright_deep",
                "error": "Playwright not installed.", "api_pagination": None,
                "intercepted_urls": [], "bypassed": False}

    async def _run():
        all_reqs, pag_reqs = [], []
        captcha_solved = False

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True, proxy=proxy,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox", "--disable-dev-shm-usage",
                        "--disable-infobars", "--window-size=1366,768",
                        "--disable-web-security",
                    ],
                )
                ctx = await browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    locale=STEALTH_CONTEXT["locale"],
                    timezone_id=STEALTH_CONTEXT["timezone_id"],
                    viewport=STEALTH_CONTEXT["viewport"],
                    java_script_enabled=True, bypass_csp=True,
                    extra_http_headers={k: v for k, v in HEADERS.items() if k != "User-Agent"},
                )
                page = await ctx.new_page()

                if STEALTH_AVAILABLE:
                    await stealth_async(page)

                await page.add_init_script(PX_INIT_SCRIPT)

                async def on_req(req):
                    u = req.url
                    all_reqs.append(u)
                    for p2 in PAGINATION_PARAM_NAMES:
                        if p2 in parse_qs(urlparse(u).query):
                            pag_reqs.append(u)
                            break
                page.on("request", on_req)

                # Initial load
                body = None
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=7_000)
                    except Exception:
                        pass
                except Exception as e:
                    await browser.close()
                    return {"body": None, "fetch_method": "playwright_deep", "error": str(e),
                            "api_pagination": None, "intercepted_urls": [], "bypassed": False}

                body = await page.content()

                # Mouse movement (PerimeterX signal)
                try:
                    await page.mouse.move(random.randint(100, 600), random.randint(100, 400))
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    await page.mouse.move(random.randint(200, 800), random.randint(200, 500))
                    await asyncio.sleep(random.uniform(0.1, 0.3))
                except Exception:
                    pass

                # CAPTCHA check
                cap_found, cap_type, sitekey = detect_captcha(body)
                if cap_found:
                    print(f"  [CAPTCHA] {cap_type} detected.", file=sys.stderr)
                    if captcha_api_key and sitekey:
                        captcha_solved = await solve_captcha_on_page(
                            page, url, cap_type, sitekey, captcha_api_key)
                        if captcha_solved:
                            body = await page.content()
                    else:
                        print("  [CAPTCHA] No key — pass --captcha-key.", file=sys.stderr)

                # Human-like scroll
                print("  [SCROLL] Human-like scroll...", file=sys.stderr)
                try:
                    ph = await page.evaluate("document.body.scrollHeight")
                    pos = 0
                    while pos < ph:
                        step = random.randint(180, 480)
                        pos = min(pos + step, ph)
                        await page.evaluate(f"window.scrollTo(0, {pos})")
                        await asyncio.sleep(random.uniform(0.3, 0.9))
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight - 800)")
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                    try:
                        await page.wait_for_load_state("networkidle", timeout=5_000)
                    except Exception:
                        pass
                    body = await page.content()
                except Exception:
                    pass

                # [FIX] Extra bottom-scroll pass for Etsy-style sites
                # where pagination only appears after ALL products have loaded.
                # After the main scroll, do a targeted scroll to the exact bottom
                # and wait specifically for pagination elements.
                pag_appeared = False
                for sel in POST_SCROLL_WAIT_SELECTORS:
                    try:
                        await page.wait_for_selector(sel, timeout=3_000)
                        print(f"  [SCROLL] Pagination appeared: {sel}", file=sys.stderr)
                        pag_appeared = True
                        break
                    except Exception:
                        continue

                # If no pagination found yet, do one more scroll-to-bottom attempt
                if not pag_appeared:
                    try:
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(2.0)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=4_000)
                        except Exception:
                            pass
                        for sel in POST_SCROLL_WAIT_SELECTORS:
                            try:
                                await page.wait_for_selector(sel, timeout=2_000)
                                print(f"  [SCROLL2] Pagination appeared on retry: {sel}", file=sys.stderr)
                                pag_appeared = True
                                break
                            except Exception:
                                continue
                    except Exception:
                        pass

                body = await page.content()
                await browser.close()

                api_pag = None
                if pag_reqs:
                    best = pag_reqs[0]
                    qs = parse_qs(urlparse(best).query)
                    for pm in PAGINATION_PARAM_NAMES:
                        if pm in qs:
                            api_pag = {"url": best, "pagination_key": pm}
                            break

                return {
                    "body": body,
                    "fetch_method": "playwright_deep",
                    "api_pagination": api_pag,
                    "intercepted_urls": pag_reqs,
                    "all_requests": all_reqs,
                    "bypassed": not _is_still_blocked(body),
                    "captcha_solved": captcha_solved,
                    "pagination_appeared": pag_appeared,
                }

        except Exception as e:
            return {"body": None, "fetch_method": "playwright_deep", "error": str(e),
                    "api_pagination": None, "intercepted_urls": [], "bypassed": False}

    try:
        return await asyncio.wait_for(_run(), timeout=90.0)
    except asyncio.TimeoutError:
        return {"body": None, "fetch_method": "playwright_deep",
                "error": "Timeout (90s).", "api_pagination": None,
                "intercepted_urls": [], "bypassed": False}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

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
        print(f"  [DEBUG] HTML -> {fname}", file=sys.stderr)
    except Exception as e:
        print(f"  [DEBUG] Write failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# MAIN ANALYSE FUNCTION
# ---------------------------------------------------------------------------

async def analyse_url(url, no_playwright=False, proxy=None,
                      captcha_api_key=None, debug=False):
    base = {
        "url": url, "block_detected": False, "block_type": None,
        "fetch_method": "requests", "pagination_found": False,
        "pagination_type": "none", "pagination_key": None,
        "max_page": None, "notes": "",
    }

    rr = fetch_with_requests(url)

    if rr.get("error"):
        base["notes"] = f"requests error: {rr['error']}"
        if no_playwright:
            return base
        pw = await fetch_with_deep_interaction(url, captcha_api_key, proxy, debug)
        if pw.get("error"):
            base["notes"] += f" | playwright error: {pw['error']}"
            return base
        if pw.get("api_pagination"):
            return _build_api_result(url, pw["api_pagination"], pw["fetch_method"])
        rr = {**rr, **pw, "needs_playwright": False, "blocked": False}

    if rr.get("blocked"):
        bt = rr["block_type"]
        print(f"  [{bt}] Launching deep interaction bypass...", file=sys.stderr)

        if no_playwright or not PLAYWRIGHT_AVAILABLE:
            base.update({"block_detected": True, "block_type": bt,
                         "notes": f"{bt}. Playwright unavailable."})
            return base

        pw = await fetch_with_deep_interaction(url, captcha_api_key, proxy, debug)

        if pw.get("error"):
            base.update({"block_detected": True, "block_type": bt,
                         "fetch_method": "playwright_deep",
                         "notes": f"{bt}. Error: {pw['error']}"})
            return base

        if not pw.get("bypassed"):
            base.update({"block_detected": True, "block_type": bt,
                         "fetch_method": "playwright_deep",
                         "notes": f"{bt}. Bypass failed. Try --proxy (residential) or --captcha-key."})
            return base

        print("  Bypass succeeded!", file=sys.stderr)
        body = pw.get("body") or ""
        fm = pw["fetch_method"]
        if pw.get("api_pagination"):
            r = _build_api_result(url, pw["api_pagination"], fm)
            r["notes"] = "[deep bypass] " + r["notes"]
            return r
        if body:
            _write_debug(debug, url, body)
            soup = BeautifulSoup(body, "html.parser")
            pag = parse_pagination(soup, url, raw_html=body)
            base.update(pag)
            base["fetch_method"] = fm
            base["notes"] = ("[deep bypass] " + base["notes"]).strip()
        return base

    body = rr.get("body") or ""
    fm = rr.get("fetch_method", "requests")

    if rr.get("needs_playwright") and not no_playwright:
        pw = await fetch_with_deep_interaction(url, captcha_api_key, proxy, debug)
        if not pw.get("error"):
            body = pw.get("body") or body
            fm = pw.get("fetch_method", "playwright_deep")
            if pw.get("api_pagination"):
                return _build_api_result(url, pw["api_pagination"], fm)
            base["notes"] = "JS framework — deep interaction fetch used."

    if body:
        _write_debug(debug, url, body)
        soup = BeautifulSoup(body, "html.parser")
        pag = parse_pagination(soup, url, raw_html=body)
        base.update(pag)
        base["fetch_method"] = fm

        if (not pag["pagination_found"] and fm == "requests"
                and PLAYWRIGHT_AVAILABLE and not no_playwright):
            print("  Static found nothing — trying deep interaction...", file=sys.stderr)
            pw = await fetch_with_deep_interaction(url, captcha_api_key, proxy, debug)
            if pw.get("api_pagination"):
                return _build_api_result(url, pw["api_pagination"], pw["fetch_method"])
            if pw.get("body"):
                _write_debug(debug, url, pw["body"], suffix="_deep")
                soup2 = BeautifulSoup(pw["body"], "html.parser")
                pag2 = parse_pagination(soup2, url, raw_html=pw["body"])
                if pag2["pagination_found"]:
                    base.update(pag2)
                    base["fetch_method"] = pw["fetch_method"]
    else:
        base["notes"] = "Empty body received."

    return base


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main():
    start_time = time.time()

    parser = argparse.ArgumentParser(
        description="Detect pagination on MRP URLs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
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
                                   debug=args.debug)
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