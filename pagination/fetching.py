"""
fetching.py
===========
HTTP and Playwright-based page fetching.
"""

import asyncio
import random
import sys
from urllib.parse import urlparse, parse_qs

from .constants import (
    HEADERS,
    PAGINATION_PARAM_NAMES,
    POST_SCROLL_WAIT_SELECTORS,
    PX_INIT_SCRIPT,
    STEALTH_CONTEXT,
)
from .dependencies import (
    CURL_CFFI_AVAILABLE,
    PLAYWRIGHT_AVAILABLE,
    STEALTH_AVAILABLE,
    curl_requests,
    std_requests,
)
from .detection import (
    _is_still_blocked,
    detect_block,
    detect_captcha,
    is_js_rendered,
)
from .captcha import solve_captcha_on_page


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


def fetch_with_scraperapi(url, scraper_key):
    try:
        import requests
        payload = { 'api_key': scraper_key, 'url': url }
        print(f"  [ScraperAPI] Fetching via ScraperAPI...", file=sys.stderr)
        resp = requests.get('https://api.scraperapi.com/', params=payload, timeout=60)
        
        body = resp.text
        h = dict(resp.headers)
        
        blocked, btype = detect_block(resp.status_code, h, body)
        if blocked:
            return {"status_code": resp.status_code, "blocked": True,
                    "block_type": btype, "body": None, "fetch_method": "scraperapi"}
        
        return {
            "status_code": resp.status_code, "blocked": False, "block_type": None,
            "body": body, "fetch_method": "scraperapi",
            "needs_playwright": (
                is_js_rendered(body)
                or len(body.strip()) < 2000
                or (len(body.strip()) < 100_000 and not any(
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
                "body": None, "fetch_method": "scraperapi", "error": str(e)}


async def fetch_with_deep_interaction(url, captcha_api_key=None, proxy=None, debug=False):
    if not PLAYWRIGHT_AVAILABLE:
        return {"body": None, "fetch_method": "playwright_deep",
                "error": "Playwright not installed.", "api_pagination": None,
                "intercepted_urls": [], "bypassed": False}

    from .dependencies import async_playwright, stealth_async

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
