"""HTTP fetching with retries, plus a headless browser for pages that need one."""
import json
import logging
import os
import signal
import subprocess
import sys
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
        retry = Retry(total=2, connect=0, read=0, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
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


MAX_BYTES = 8_000_000


def fetch(url, timeout=None, **kw):
    """GET with a hard wall-clock limit and size cap. A plain requests timeout only bounds
    each socket read, so a server that trickles bytes or streams forever (radio, video)
    would otherwise hold the crawl indefinitely."""
    timeout = timeout or config.TIMEOUT
    try:
        with session().get(url, timeout=(8, timeout), allow_redirects=True, stream=True, **kw) as r:
            ctype = r.headers.get("content-type", "")
            text = ""
            if "html" in ctype or "xml" in ctype or "json" in ctype or not ctype:
                end, body = time.time() + timeout * 2, bytearray()
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > MAX_BYTES or time.time() > end:
                        break
                text = body.decode(r.encoding or "utf-8", errors="replace")
            return Page(url, r.status_code, text, dict(r.headers), r.url)
    except requests.RequestException as e:
        log.debug("fetch failed %s: %s", url, e)
        return Page(url, None, "", {}, url)


def fetch_json(url, timeout=None, **kw):
    page = fetch(url, timeout, **kw)
    if page.status == 200:
        try:
            return json.loads(page.html)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- browser
# Each render runs Chromium in its own child process with a hard time limit, so one
# page that hangs the browser can never stall the whole crawl. BROWSERS caps how many
# run at once.

_slots = threading.BoundedSemaphore(config.BROWSERS)
RENDER_LIMIT = 100  # seconds per page, including browser start-up

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


def render(url, max_scrolls=60, max_clicks=15):
    """Scroll and click the page in headless Chromium (see _render) in a child process
    that is killed if it runs past RENDER_LIMIT."""
    if not config.USE_BROWSER:
        return None
    with _slots:
        proc = subprocess.Popen([sys.executable, "-m", "prospector.net", url, str(max_scrolls), str(max_clicks)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                                cwd=config.ROOT)
        try:
            out, _ = proc.communicate(timeout=RENDER_LIMIT)
        except subprocess.TimeoutExpired:
            log.info("render timed out, killed: %s", url)
            _kill(proc)
            return None
        finally:
            _kill(proc)  # also takes down any Chromium left behind
    try:
        d = json.loads(out)
    except ValueError:
        return None
    return Page(url, d["status"], d["html"], {}, d["final_url"], rendered=True) if d else None


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _render(url, max_scrolls, max_clicks):
    """Open the page in headless Chromium, scroll to the very bottom until the page
    stops growing, click any 'load more comments' / 'leave a comment' style buttons,
    and return the rendered HTML (including same-origin iframes such as comment forms)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        try:
            return _render_in(browser, url, max_scrolls, max_clicks)
        finally:
            browser.close()


def _render_in(browser, url, max_scrolls, max_clicks):
    ctx = browser.new_context(user_agent=config.USER_AGENT, viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,mp4,webm}", lambda r: r.abort())
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=config.TIMEOUT * 2000)
        status = resp.status if resp else None
        last_h, stable, clicks = 0, 0, 0
        deadline = time.time() + 60
        for _ in range(max_scrolls):
            if time.time() > deadline:
                break
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
    """Nothing to clean up: every render's Chromium exits with its child process."""


if __name__ == "__main__":
    _p = _render(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
    sys.stdout.write(json.dumps(_p and {"status": _p.status, "html": _p.html, "final_url": _p.final_url}))
