"""Harvest every outbound link from known spammed pages, so the link builders behind them
can be looked up in Semrush. Reads only; never posts anything."""
import csv
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import comments, config, domains, net

log = logging.getLogger(__name__)


def _page_links(url):
    """Every external link on the page itself, wherever it sits (comments, footer, injected spam)."""
    page = net.fetch(url)
    if not page.ok:
        return []
    own = domains.root_domain(page.final_url)
    out = []
    for a in BeautifulSoup(page.html, "html.parser").find_all("a", href=True):
        href = urljoin(page.final_url, a["href"])
        if not href.startswith("http"):
            continue
        d = domains.root_domain(href)
        if not d or d == own or domains.is_blocked(d):
            continue
        rel = " ".join(sorted(comments._rel(a))) or "follow"
        out.append({"kind": "page", "anchor": a.get_text(" ", strip=True)[:120], "url": href,
                    "domain": d, "rel": rel})
    return out


def _harvest_one(url):
    try:
        r = comments.check_page(url)
        links = [dict(l, kind=l["kind"]) for l in r["links"]]
    except Exception as e:  # one bad page must not stop the rest
        log.warning("check failed for %s: %s", url, e)
        r, links = {"comments_open": None}, []
    seen = {l["url"] for l in links}
    links += [l for l in _page_links(url) if l["url"] not in seen]
    for l in links:
        l["domain"] = l.get("domain") or domains.root_domain(l["url"])
    return url, r.get("comments_open"), links


def run(url_file):
    urls = [u.strip() for u in open(url_file, encoding="utf-8") if u.strip() and not u.startswith("#")]
    log.info("harvest: %d pages", len(urls))
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows, by_domain = [], defaultdict(lambda: {"pages": set(), "anchors": set(), "kinds": set()})
    with ThreadPoolExecutor(config.WORKERS) as ex:
        for url, open_, links in ex.map(_harvest_one, urls):
            log.info("harvest: %s -> %d links", url, len(links))
            for l in links:
                rows.append({"page_url": url, "comments_open": open_, "kind": l["kind"],
                             "anchor": l.get("anchor", ""), "url": l["url"], "domain": l["domain"],
                             "rel": l.get("rel", "")})
                d = by_domain[l["domain"]]
                d["pages"].add(url)
                d["kinds"].add(l["kind"])
                if l.get("anchor"):
                    d["anchors"].add(l["anchor"][:60])
    with open(config.OUT_DIR / "harvest_links.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["page_url", "comments_open", "kind", "anchor", "url", "domain", "rel"])
        w.writeheader()
        w.writerows(rows)
    with open(config.OUT_DIR / "harvest_domains.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["domain", "pages", "kinds", "anchors", "example_page"])
        for d, s in sorted(by_domain.items(), key=lambda kv: -len(kv[1]["pages"])):
            w.writerow([d, len(s["pages"]), " ".join(sorted(s["kinds"])),
                        " | ".join(sorted(s["anchors"])[:5]), sorted(s["pages"])[0]])
    log.info("harvest: %d links, %d domains", len(rows), len(by_domain))
