"""
detection.py
============
Block detection, CAPTCHA detection, and JS-framework detection helpers.
"""

import re

from .constants import (
    BLOCK_BODY_SIGNALS,
    CLOUDFLARE_SIGNALS,
    JS_FRAMEWORK_MARKERS,
    PERIMETERX_SIGNALS,
)


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
        m = re.search(r'data-sitekey=["\'](^"\']+)["\']', body)
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
