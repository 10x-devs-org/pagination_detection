"""
captcha.py
==========
CAPTCHA solving via 2captcha integration.
"""

import asyncio
import sys

from .dependencies import CAPTCHA_SOLVER_AVAILABLE, TwoCaptcha


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
