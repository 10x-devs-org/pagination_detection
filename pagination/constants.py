"""
constants.py
============
All shared constants used across the pagination detection modules.
"""

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
    "currentPage", "current_page", "cp", "pn",
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
