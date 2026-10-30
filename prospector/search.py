"""Stage 1: find the 'dofollow blog commenting list' listicles.

Backends are tried in order: Serper, Brave, Google CSE (each only if its key is set),
then keyless HTML scraping of DuckDuckGo and Bing. config/seeds.txt is always included,
so a run still has listicles to work from if every search engine refuses."""
import logging
import re
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from bs4 import BeautifulSoup

from . import config, net

log = logging.getLogger(__name__)


def _serper(q, n):
    r = net.session().post("https://google.serper.dev/search", timeout=config.TIMEOUT,
                           json={"q": q, "num": n}, headers={"X-API-KEY": config.SERPER_API_KEY})
    return [o["link"] for o in r.json().get("organic", [])]


def _brave(q, n):
    data = net.fetch_json(f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(q)}&count={n}",
                          headers={"X-Subscription-Token": config.BRAVE_API_KEY, "Accept": "application/json"})
    return [o["url"] for o in (data or {}).get("web", {}).get("results", [])]


def _google_cse(q, n):
    data = net.fetch_json("https://www.googleapis.com/customsearch/v1?key=%s&cx=%s&q=%s&num=%d"
                          % (config.GOOGLE_CSE_KEY, config.GOOGLE_CSE_CX, quote_plus(q), min(n, 10)))
    return [o["link"] for o in (data or {}).get("items", [])]


def _ddg(q, n):
    page = net.fetch("https://html.duckduckgo.com/html/?q=" + quote_plus(q))
    soup = BeautifulSoup(page.html, "lxml")
    out = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlsplit(href).query).get("uddg", [""])[0])
        if href.startswith("http"):
            out.append(href)
    return out[:n]


def _bing(q, n):
    page = net.fetch(f"https://www.bing.com/search?q={quote_plus(q)}&count={n}")
    soup = BeautifulSoup(page.html, "lxml")
    out = []
    for a in soup.select("li.b_algo h2 a"):
        href = a.get("href", "")
        m = re.search(r"[?&]u=a1([^&]+)", href)  # bing click-tracking wrapper
        if m:
            import base64
            try:
                href = base64.urlsafe_b64decode(m.group(1) + "==").decode()
            except Exception:
                continue
        if href.startswith("http"):
            out.append(href)
    return out[:n]


def backends():
    b = []
    if config.SERPER_API_KEY:
        b.append(("serper", _serper))
    if config.BRAVE_API_KEY:
        b.append(("brave", _brave))
    if config.GOOGLE_CSE_KEY and config.GOOGLE_CSE_CX:
        b.append(("google_cse", _google_cse))
    b += [("duckduckgo", _ddg), ("bing", _bing)]
    return b


def search(query, n=None):
    n = n or config.MAX_SEARCH_RESULTS
    for name, fn in backends():
        try:
            urls = fn(query, n)
        except Exception as e:
            log.info("search backend %s failed: %s", name, e)
            continue
        if urls:
            log.info("search [%s] %r -> %d results", name, query, len(urls))
            return urls
    log.warning("no search backend returned results for %r", query)
    return []


def find_listicles():
    """Returns [(url, origin)] where origin is the query or 'seeds.txt'."""
    seen, out = set(), []
    for url in config.read_list("seeds.txt"):
        if url not in seen:
            seen.add(url)
            out.append((url, "seeds.txt"))
    for q in config.read_list("queries.txt"):
        for url in search(q):
            if url not in seen:
                seen.add(url)
                out.append((url, q))
    return out[: config.MAX_LISTICLES]
