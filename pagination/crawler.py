import urllib.parse
import re

def create_next_template(base_url, href, key):
    """
    Creates a URL template (with '{page}' placeholder) from a Next button href.
    Works for both query parameters (?page=2) and path parameters (/pn/2).
    """
    if not href or not key:
        return None

    full_href = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(full_href)

    # 1. Check Path parameters (/page/2, /pn/2, page-2.html)
    path_pat = re.search(r"/(" + re.escape(key) + r")[-/](\d+)", parsed.path, re.I)
    if path_pat:
        new_path = parsed.path[:path_pat.start(2)] + "{page}" + parsed.path[path_pat.end(2):]
        return parsed._replace(path=new_path).geturl()

    # 2. Check Query Parameters (?page=2)
    qs = urllib.parse.parse_qs(parsed.query)
    # Rebuild query manually to preserve structure and insert placeholder
    query_parts = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    new_query_parts = []
    replaced = False

    for k, v in query_parts:
        if k.lower() == key.lower():
            new_query_parts.append((k, "{page}"))
            replaced = True
        else:
            new_query_parts.append((k, v))
            
    if replaced:
        new_query = urllib.parse.urlencode(new_query_parts, safe="{}")
        return parsed._replace(query=new_query).geturl()

    return None

def _count_products(soup):
    """
    Counts product-like entries to ensure the page is actually populated.
    """
    # 1. Broadly used selectors
    selectors = [
        '[data-selenium="listingProductTag"]', # B&H
        '[data-component-type="s-result-item"]', # Amazon
        '.product-card', '.product-item', '.v2-listing-card',
        '[data-testid="product-card"]',
        'li.product', 'div.product',
        'a[href*="/p/"]', 'a[href*="/pd/"]', 'a[href*="/product/"]'
    ]
    
    total = 0
    for sel in selectors:
        nodes = soup.select(sel)
        if nodes:
            total = max(total, len(nodes))
            
    # Fallback: check if we see any price-like patterns if selectors fail
    if total == 0:
        if soup.find(string=re.compile(r"\$\d+\.\d{2}")):
            return 1 # assumed present
            
    return total

def _get_page_fingerprint(soup):
    """
    Returns a small hash-like string identifier for the products on this page 
    to detect if we've been redirected back to Page 1 or are seeing duplicates.
    """
    selectors = [
        '[data-selenium="listingProductTag"]',
        '[data-selenium="miniProductPage"]',
        '[data-component-type="s-result-item"]',
        'h3 a.title', 'a.product-name', '.product-card h3'
    ]
    items = []
    for sel in selectors:
        nodes = soup.select(sel)
        if nodes:
            # Get text from the first 5 items
            items = [n.get_text(strip=True)[:100] for n in nodes[:5]]
            break
            
    return "|".join(items) if items else ""

def _count_products(soup):
    """
    Counts product-like entries to ensure the page is actually populated.
    """
    # 1. Targeted selectors
    selectors = [
        '[data-selenium="listingProductTag"]', # B&H
        '[data-selenium="miniProductPage"]',   # B&H
        '[data-component-type="s-result-item"]', # Amazon
        '.product-card', '.product-item', '.v2-listing-card',
        '[data-testid="product-card"]',
        'li.product', 'div.product'
    ]
    # Removed broader "a[href*='/product/']" as it causes false positives on B&H
    
    total = 0
    for sel in selectors:
        nodes = soup.select(sel)
        if nodes:
            total = max(total, len(nodes))
            
    # Fallback: check if we see any price-like patterns if selectors fail
    if total == 0:
        if soup.find(string=re.compile(r"\$\d+\.\d{2}")):
            return 1 # assumed present
            
    return total

def verify_page_exists(fetch_func, template_url, test_num, url, baseline_fingerprint=None):
    """
    Fetches the test URL and uses parsing to see if the page is a valid continuation.
    Returns True if valid, False if not.
    """
    from bs4 import BeautifulSoup
    from pagination.parsing import parse_pagination

    test_url = template_url.replace("{page}", str(test_num))
    print(f"  [BinaryCrawler] Probing page {test_num}...")
    
    try:
        raw, html, _, blocked, _ = fetch_func(test_url)
        if blocked:
            print(f"    [BinaryCrawler] Page {test_num} failed: Blocked.")
            return False
            
        if not html or len(html) < 500:
            print(f"    [BinaryCrawler] Page {test_num} failed: Body empty.")
            return False

        soup = BeautifulSoup(html, "html.parser")
        
        # 1. Check for actual products
        item_count = _count_products(soup)
        if item_count == 0:
            print(f"    [BinaryCrawler] Page {test_num} failed: 0 products found.")
            return False
            
        # 2. Check for "Page 1 Redirection" via fingerprint comparison
        if baseline_fingerprint is not None:
             fingerprint = _get_page_fingerprint(soup)
             if fingerprint and fingerprint == baseline_fingerprint:
                 print(f"    [BinaryCrawler] Page {test_num} failed: Content is identical to Page 1 (silent redirect).")
                 return False

        # 3. Double check pagination existence
        res = parse_pagination(soup, test_url, raw or html)
        if not res["pagination_found"]:
            print(f"    [BinaryCrawler] Page {test_num} failed: No pagination bar.")
            return False

        print(f"    [BinaryCrawler] Page {test_num} verified ({item_count} items).")
        return True
    except Exception as e:
        print(f"    [BinaryCrawler] Page {test_num} error: {e}")
        return False

def binary_search_max_page(start_num, template_url, fetch_func, url):
    """
    Executes a fast binary search testing bounds to find the true max_page dynamically.
    """
    from bs4 import BeautifulSoup
    print(f"  [BinaryCrawler] Launching deep binary search... (Starting at {start_num})")
    
    # [NEW] Get Page 1 Fingerprint as baseline
    print("  [BinaryCrawler] Capturing Page 1 fingerprint for redirect detection...")
    raw1, html1, _, _, _ = fetch_func(url)
    soup1 = BeautifulSoup(html1 or raw1 or "", "html.parser")
    baseline = _get_page_fingerprint(soup1)
    
    low = start_num
    high = start_num * 2
    
    # 1. Exponential Bound Search
    while True:
        if verify_page_exists(fetch_func, template_url, high, url, baseline_fingerprint=baseline):
            low = high
            high = high * 2
            if high > 3000:  # Safety ceiling
                return 3000
        else:
            break
            
    # 2. Binary Search between Low and High
    # If high is invalid, true max is between low and high-1
    result = low
    high = high - 1 
    
    while low <= high:
        mid = (low + high) // 2
        if verify_page_exists(fetch_func, template_url, mid, url, baseline_fingerprint=baseline):
            result = mid
            low = mid + 1
        else:
            high = mid - 1
            
    print(f"  [BinaryCrawler] Successfully found true max_page -> {result}")
    return result
