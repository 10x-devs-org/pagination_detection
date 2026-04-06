"""
parsing.py
==========
Pagination parsing helpers and the main parse_pagination function.
"""

import math
import re
from urllib.parse import urlparse, parse_qs

from .constants import (
    DEFAULT_ITEMS_PER_PAGE,
    INFINITE_SCROLL_STRONG_SIGNALS,
    INFINITE_SCROLL_WEAK_SIGNALS,
    LOAD_MORE_EXCLUSION_KEYWORDS,
    LOAD_MORE_SELECTORS,
    NON_PRODUCT_CONTAINERS,
    PAGINATION_PARAM_NAMES,
    PAGINATION_SELECTORS,
)


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
    for pat in [r"/(page|p|pg|pn)/(\d+)", r"/(page|p|pg|pn)-(\d+)(?:\.html?)?"]:
        m = re.search(pat, path, re.I)
        if m:
            return m.group(1).lower(), int(m.group(2))
    m = re.match(r"^(page|p|pg|pn)-(\d+)(?:\.html?)?$",
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
    """
    Detects 'Load More' or 'View More' buttons and tries to extract a next URL.
    """
    # 1. Search for <a> links that act as Load More buttons
    for a in soup.find_all("a", href=True):
        t = a.get_text().lower()
        if "load more" in t or "view more" in t:
             if not _element_in_non_product_container(a, soup):
                 return {"found": True, "text": a.get_text(strip=True), "href": a["href"]}

    # 2. Search for plain <button> or <div> triggers
    for tag in ["button", "div", "span"]:
        for el in soup.find_all(tag, string=re.compile(r"load more|view more", re.I)):
            if not _element_in_non_product_container(el, soup):
                return {"found": True, "text": el.get_text(strip=True), "href": None}
    
    return {"found": False, "text": None, "href": None}


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

    # Pattern 3: B&H specific data-selenium tag
    bh_total = soup.find(attrs={"data-selenium": "total-results"})
    if bh_total:
        try:
            z = int(bh_total.get_text(strip=True).replace(",", ""))
            # B&H shows 24 items per page by default
            if z > 0:
                t = math.ceil(z / 24)
                return t, 24, z
        except Exception:
            pass

    # Pattern 4: ASOS specific "viewed X of Y products"
    m = re.search(r'viewed\s+(\d+)\s+of\s+([\d,]+)\s+products', text, re.I)
    if m:
        try:
            x = int(m.group(1).replace(",", ""))
            z = int(m.group(2).replace(",", ""))
            if x > 0 and z > 0:
                # x is per_page in the first view
                return math.ceil(z / x), x, z
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
        "next_button_href": None,
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
        
        # [UPGRADE] If Load More button has an href with a page key, treat it as link-based
        if lm["href"]:
            # Check for keys in the load more link
            import urllib.parse as urlparse
            parsed = urlparse.urlparse(lm["href"])
            qs = urlparse.parse_qs(parsed.query)
            best_key = None
            for key in ["page", "p", "page_number", "pag", "pn", "offset", "start"]:
                if key in qs:
                    best_key = key
                    break
            
            if best_key:
                r.update({
                    "pagination_found": True,
                    "pagination_type": "link",
                    "pagination_key": best_key,
                    "max_page": max_p,
                    "next_button_href": lm["href"],
                    "notes": (
                        f"Load More button used as Link trigger: '{lm['text']}'"
                        + (f" — estimated {max_p} pages ({total} results / {per_p} per page)"
                           if max_p else "")
                    ),
                })
                return r

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
        if ps_key and ps_num and (in_c or re.search(r"/(page|p|pg|pn)[-/]", href, re.I)):
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

    # Option 1 Mode: Detect if a "Next" button implies pagination continues 
    # indefinitely beyond the visibly numbered links.
    for a in anchors:
        tl = a.get_text(strip=True).lower()
        if (tl in ("next", "next »", "next page", "›", ">", "next >", "next ›", "»") 
            or any(kw in "".join(a.get("class", [])).lower() for kw in ["next", "pager-next", "pagination-next"])
            or "next" in a.get("data-selenium", "").lower()
            or a.get("rel") == ["next"]):
            r["next_button_href"] = a.get("href")
            break


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
            nxt = r.get("next_button_href")
            # If there's a Next button, check if it implies we should go BEYOND 'hi'.
            is_truncated = False
            if nxt and nxt not in ("#", "javascript:void(0);"):
                # Extract page from Next button
                k = r.get("pagination_key")
                npn = None
                if k:
                    qs = parse_qs(urlparse(nxt).query)
                    ivs = _ints(" ".join(qs.get(k, [])))
                    if ivs: npn = max(ivs)
                if not npn:
                    _, path_pn = _path_page(nxt)
                    if path_pn: npn = path_pn
                
                if npn and npn > hi:
                    is_truncated = True
                elif not npn:
                    # If we can't tell the page number of 'Next', assume truncation
                    # ONLY if hi looks like a standard truncation point (e.g. multiples of 10)
                    if hi % 10 == 0 or hi in (8, 12, 16, 24, 32, 48, 50):
                        is_truncated = True

            if is_truncated:
                r["max_page"] = None
                r["notes"] = f"Container shows numbers up to {hi}, but 'Next' button implies more (truncated). Set to None."
                return r
            
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
