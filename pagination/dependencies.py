"""
dependencies.py
===============
Guarded optional imports + availability flags.
All names are always defined (set to None when unavailable)
so that sibling modules can safely import them.
"""

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
    async_playwright = None
    PLAYWRIGHT_AVAILABLE = False

# playwright-stealth
try:
    from playwright_stealth import stealth_async
    STEALTH_AVAILABLE = True
except ImportError:
    stealth_async = None
    STEALTH_AVAILABLE = False

# 2captcha
try:
    from twocaptcha import TwoCaptcha
    CAPTCHA_SOLVER_AVAILABLE = True
except ImportError:
    TwoCaptcha = None
    CAPTCHA_SOLVER_AVAILABLE = False
