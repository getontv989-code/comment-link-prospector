"""Stage 5: free pre-scoring so only a handful of domains ever reach Semrush.

Signals (all free):
  - how many distinct comment pages the domain was seen on (active comment link builders
    show up again and again, and they are the ones with long comment backlink profiles)
  - how many of those links were dofollow
  - Tranco top-1M rank (no key needed) and Open PageRank (free key, optional)
  - whether the site is live
"""
import csv
import io
import logging
import math
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

from . import config, net

log = logging.getLogger(__name__)
TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"
_tranco = None


def tranco():
    """{domain: rank}. Cached in state/ for a week."""
    global _tranco
    if _tranco is not None:
        return _tranco
    _tranco = {}
    if not config.USE_TRANCO:
        return _tranco
    path = config.STATE_DIR / "tranco.csv.zip"
    try:
        if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
            r = net.session().get(TRANCO_URL, timeout=120)
            if r.status_code == 200:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(r.content)
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as f:
                for row in csv.reader(io.TextIOWrapper(f, encoding="utf-8")):
                    _tranco[row[1].lower()] = int(row[0])
    except Exception as e:
        log.warning("Tranco list unavailable: %s", e)
    return _tranco


def open_pagerank(domains_):
    """{domain: 0-10 decimal score}. Needs OPENPAGERANK_API_KEY (free at openpagerank.com)."""
    out = {}
    if not config.OPENPAGERANK_API_KEY:
        return out
    domains_ = list(domains_)
    for i in range(0, len(domains_), 100):
        chunk = domains_[i:i + 100]
        params = [("domains[]", d) for d in chunk]
        try:
            r = net.session().get("https://openpagerank.com/api/v1.0/getPageRank", params=params,
                                  headers={"API-OPR": config.OPENPAGERANK_API_KEY}, timeout=30)
            for row in r.json().get("response", []):
                if row.get("status_code") == 200:
                    out[row["domain"].lower()] = float(row.get("page_rank_decimal") or 0)
        except Exception as e:
            log.warning("Open PageRank failed: %s", e)
    return out


def authority(domain, opr=None):
    """0-100 free authority estimate: Open PageRank x10 when we have it, otherwise from Tranco rank.
    It is a rough stand-in for DA, good enough to rank prospects before paying for real metrics."""
    if opr and domain in opr:
        return round(opr[domain] * 10)
    rank = tranco().get(domain)
    if not rank:
        return 0
    return max(0, min(100, round(100 - 10 * math.log10(rank))))  # #1 -> 100, #1M -> 40


def is_live(domain):
    for scheme in ("https://", "http://"):
        page = net.fetch(scheme + domain, timeout=12)
        if page.status and page.status < 400:
            return True
    return False


COMMERCIAL = re.compile(
    r"\b(best|top|cheap|buy|online|service|services|agency|company|repair|near me|in [a-z]+|seo|"
    r"backlinks?|price|cost|shop|store|rental|hire|clinic|dentist|lawyer|attorney|plumber|"
    r"loan|casino|bet|insurance|course|training|institute|school|hospital|hotel|tour|packages?|"
    r"digital marketing|web design|development|software|app|review|reviews|deals?|sale)\b", re.I)


def is_keyword_anchor(anchor, domain, kind="author"):
    """Link builders comment under a money keyword ('best dentist in Pune'), real readers
    under their own name. Anchors that are just the domain or a short name don't count.
    A phrase linked inside the comment text ('Website usability testing') is the classic
    spam pattern, so two words are enough there."""
    a = (anchor or "").strip()
    if not a or a.lower().startswith(("http", "www.")) or domain.split(".")[0] in a.lower().replace(" ", ""):
        return False
    return bool(COMMERCIAL.search(a)) or len(a.split()) >= (2 if kind == "body" else 4)


def score_domains(stats):
    """stats: {domain: {"pages": set, "dofollow_pages": set}} -> sorted list of dict rows."""
    names = list(stats)
    log.info("stage 5: scoring %d commenter domains", len(names))
    opr = open_pagerank(names)
    with ThreadPoolExecutor(config.WORKERS) as ex:
        live = dict(zip(names, ex.map(is_live, names)))
    rows = []
    for d in names:
        s = stats[d]
        auth = authority(d, opr)
        pages, dof = len(s["pages"]), len(s["dofollow_pages"])
        lst, kw = len(s["listicle_pages"]), len(s["keyword_anchor_pages"])
        # Commenting on the 'dofollow list' posts themselves, or under a money keyword,
        # is what separates link builders from real readers.
        score = (10 * pages + 5 * dof + 15 * lst + 8 * kw + auth / 5) if live[d] else 0
        rows.append({
            "domain": d, "pages_seen": pages, "dofollow_pages": dof,
            "on_seo_list_pages": lst, "keyword_anchor_pages": kw,
            "tranco_rank": tranco().get(d, ""), "open_pagerank": opr.get(d, ""),
            "authority": auth, "live": live[d], "score": round(score, 1),
            "example_pages": " | ".join(sorted(s["pages"])[:3]),
        })
    rows.sort(key=lambda r: -r["score"])
    return rows
