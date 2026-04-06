"""
analyse.py
==========
Main analyse_url orchestration function.
"""

import sys

from bs4 import BeautifulSoup

from .dependencies import PLAYWRIGHT_AVAILABLE
from .fetching import fetch_with_requests, fetch_with_deep_interaction, fetch_with_scraperapi
from .helpers import _build_api_result, _write_debug
from .parsing import parse_pagination


async def analyse_url(url, no_playwright=False, proxy=None,
                      captcha_api_key=None, debug=False, scraper_key=None, no_binary_crawler=False):
    from .crawler import create_next_template, binary_search_max_page

    base = {
        "url": url, "block_detected": False, "block_type": None,
        "fetch_method": "scraperapi" if scraper_key else "requests", "pagination_found": False,
        "pagination_type": "none", "pagination_key": None,
        "max_page": None, "notes": "",
    }

    if scraper_key:
        rr = fetch_with_scraperapi(url, scraper_key)
    else:
        rr = fetch_with_requests(url)

    if rr.get("error"):
        base["notes"] = f"{base['fetch_method']} error: {rr['error']}"
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
    fm = rr.get("fetch_method", "scraperapi" if scraper_key else "requests")

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

        if (not pag["pagination_found"] and fm in ("requests", "scraperapi")
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

    # -----------------------------------------------------
    # OPTION B: Binary Search Crawler Hook
    # -----------------------------------------------------
    if base.get("max_page") is None and base.get("next_button_href") and not no_binary_crawler:
        from .crawler import create_next_template, binary_search_max_page

        template = create_next_template(url, base["next_button_href"], base.get("pagination_key"))
        if template:
            def proxy_fetch(t_url):
                # Import here to avoid circular imports during startup
                from .fetching import fetch_with_requests, fetch_with_scraperapi
                if scraper_key:
                    res = fetch_with_scraperapi(t_url, scraper_key)
                else:
                    res = fetch_with_requests(t_url)
                
                body = res.get("body") or ""
                blocked = res.get("blocked", False)
                return body, body, None, blocked, None
                
            found_max = binary_search_max_page(8, template, proxy_fetch, url)
            if found_max:
                base["max_page"] = found_max
                base["notes"] = f"Resolved via Binary Crawler -> True max page is {found_max}."

    if "next_button_href" in base:
        del base["next_button_href"]

    return base
