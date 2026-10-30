"""HTTP fetching with retries, plus a lazily started headless browser."""
import logging
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

log = logging.getLogger(__name__)
_local = threading.local()


def session():
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(total=2, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET", "HEAD"))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _local.session = s
    return s


class Page:
    def __init__(self, url, status, html, headers=None, final_url=None, rendered=False):
        self.url = url
        self.status = status
        self.html = html or ""
        self.headers = headers or {}
        self.final_url = final_url or url
        self.rendered = rendered

    @property
    def ok(self):
        return self.status is not None and 200 <= self.status < 400 and bool(self.html)


def fetch(url, timeout=None, **kw):
    try:
        r = session().get(url, timeout=timeout or config.TIMEOUT, allow_redirects=True, **kw)
        ctype = r.headers.get("content-type", "")
        text = r.text if ("html" in ctype or "xml" in ctype or "json" in ctype or not ctype) else ""
        return Page(url, r.status_code, text, dict(r.headers), r.url)
    except requests.RequestException as e:
        log.debug("fetch failed %s: %s", url, e)
        return Page(url, None, "", {}, url)


def fetch_json(url, timeout=None, **kw):
    try:
        r = session().get(url, timeout=timeout or config.TIMEOUT, **kw)
        if r.status_code == 200:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


# ---------------------------------------------------------------- browser
# Playwright is only started if a page actually needs it, and is used from one
# thread at a time (sync API objects are not thread-safe).

_browser_lock = threading.Lock()
_pw = None
_browser = None

# Clicks in-page buttons that reveal comments or the comment form. Links that would
# navigate away are skipped, so the crawler never leaves the page or submits anything.
_CLICK_JS = r"""
() => {
  const re = /(load|show|view|see)\s+(more|all|older|previous)?\s*(comments?|replies)|(leave|post|add|write)\s+(a\s+)?(comment|reply)|^\s*\d*\s*comments?\s*(\(\d+\))?\s*$/i;
  const els = [...document.querySelectorAll('button, a, [role=button], span.wpdiscuz-loadmore, div.wpd-load-more-submit')];
  for (const el of els) {
    if (el.dataset.prospectorClicked) continue;
    const t = (el.innerText || el.value || '').trim();
    if (!t || t.length > 60 || !re.test(t)) continue;
    if (el.closest('form')) continue;  // never touch submit buttons
    const href = el.getAttribute('href');
    if (el.tagName === 'A' && href && !href.startsWith('#') && !href.startsWith('javascript')) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    el.dataset.prospectorClicked = '1';
    el.click();
    return t;
  }
  return null;
}
"""


def _get_browser():
    global _pw, _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(args=["--no-sandbox"])
    return _browser


def render(url, max_scrolls=60, max_clicks=15):
    """Open the page in headless Chromium, scroll to the very bottom until the page
    stops growing, click any 'load more comments' / 'leave a comment' style buttons,
    and return the rendered HTML (including same-origin iframes such as comment forms)."""
    if not config.USE_BROWSER:
        return None
    with _browser_lock:
        try:
            browser = _get_browser()
        except Exception as e:  # Playwright not installed
            log.warning("headless browser unavailable: %s", e)
            return None
        ctx = browser.new_context(user_agent=config.USER_AGENT, viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,mp4,webm}", lambda r: r.abort())
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=config.TIMEOUT * 2000)
            status = resp.status if resp else None
            last_h, stable, clicks = 0, 0, 0
            for _ in range(max_scrolls):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(700)
                if clicks < max_clicks and page.evaluate(_CLICK_JS):
                    clicks += 1
                    page.wait_for_timeout(1200)
                    stable = 0
                    continue
                h = page.evaluate("document.body.scrollHeight")
                stable = stable + 1 if h == last_h else 0
                last_h = h
                if stable >= 3:
                    break
            html = page.content()
            frames = []
            for fr in page.frames[1:]:
                try:
                    if any(k in (fr.url or "") for k in ("comment", "disqus", "blogger.com")):
                        frames.append(f'<div data-iframe-src="{fr.url}">{fr.content()}</div>')
                except Exception:
                    pass
            if frames:
                html = html.replace("</body>", "".join(frames) + "</body>")
            return Page(url, status, html, {}, page.url, rendered=True)
        except Exception as e:
            log.debug("render failed %s: %s", url, e)
            return None
        finally:
            ctx.close()


def close_browser():
    global _pw, _browser
    if _browser is not None:
        try:
            _browser.close()
            _pw.stop()
        except Exception:
            pass
        _browser = None
        _pw = None


def polite_sleep(seconds=0.5):
    time.sleep(seconds)
