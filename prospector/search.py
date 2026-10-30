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
    return [(o["link"], o.get("title", "")) for o in r.json().get("organic", [])]


def _brave(q, n):
    data = net.fetch_json(f"https://api.search.brave.com/res/v1/web/search?q={quote_plus(q)}&count={n}",
                          headers={"X-Subscription-Token": config.BRAVE_API_KEY, "Accept": "application/json"})
    return [(o["url"], o.get("title", "")) for o in (data or {}).get("web", {}).get("results", [])]


def _google_cse(q, n):
    data = net.fetch_json("https://www.googleapis.com/customsearch/v1?key=%s&cx=%s&q=%s&num=%d"
                          % (config.GOOGLE_CSE_KEY, config.GOOGLE_CSE_CX, quote_plus(q), min(n, 10)))
    return [(o["link"], o.get("title", "")) for o in (data or {}).get("items", [])]


def _ddg(q, n):
    page = net.fetch("https://html.duckduckgo.com/html/?q=" + quote_plus(q))
    soup = BeautifulSoup(page.html, "lxml")
    out = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlsplit(href).query).get("uddg", [""])[0])
        if href.startswith("http"):
            out.append((href, a.get_text(" ", strip=True)))
    return out[:n]


def _ddg_lite(q, n):
    r = net.session().post("https://lite.duckduckgo.com/lite/", data={"q": q}, timeout=config.TIMEOUT)
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.select("a.result-link, a[href^='http']"):
        href = a.get("href", "")
        if "uddg=" in href:
            href = unquote(parse_qs(urlsplit(href).query).get("uddg", [""])[0])
        if href.startswith("http") and "duckduckgo.com" not in href:
            out.append((href, a.get_text(" ", strip=True)))
    return out[:n]


def _mojeek(q, n):
    page = net.fetch("https://www.mojeek.com/search?q=" + quote_plus(q))
    soup = BeautifulSoup(page.html, "lxml")
    return [(a["href"], a.get_text(" ", strip=True)) for a in soup.select("ul.results-standard li h2 a[href], a.title[href]")
            if a["href"].startswith("http")][:n]


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
            out.append((href, a.get_text(" ", strip=True)))
    return out[:n]


def backends():
    b = []
    if config.SERPER_API_KEY:
        b.append(("serper", _serper))
    if config.BRAVE_API_KEY:
        b.append(("brave", _brave))
    if config.GOOGLE_CSE_KEY and config.GOOGLE_CSE_CX:
        b.append(("google_cse", _google_cse))
    b += [("duckduckgo", _ddg), ("duckduckgo_lite", _ddg_lite), ("mojeek", _mojeek), ("bing", _bing)]
    return b


STOP = {"the", "and", "for", "list", "sites", "site", "with", "inurl", "a", "of", "to", "in"}


def relevant(query, results):
    """Keyless scraping sometimes gets a page of results for a different query (Bing does this
    to datacenter IPs). Accept a result set only if most titles share a word with the query."""
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower().replace("do follow", "dofollow"))
             if len(w) > 2 and w not in STOP}
    if not words or not results:
        return bool(results)
    hits = sum(1 for url, title in results
               if words & set(re.findall(r"[a-z0-9]+", (title + " " + url).lower())))
    return hits >= max(1, len(results) // 2)


def search(query, n=None):
    n = n or config.MAX_SEARCH_RESULTS
    for name, fn in backends():
        try:
            results = fn(query, n)
        except Exception as e:
            log.info("search backend %s failed: %s", name, e)
            continue
        if results and relevant(query, results):
            log.info("search [%s] %r -> %d results", name, query, len(results))
            return [u for u, _ in results]
        if results:
            log.info("search [%s] %r -> off-topic results, trying next engine", name, query)
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
