"""Runs stages 1-6 end to end and writes the CSV outputs. Prospecting only: nothing is
ever posted or submitted to any site."""
import csv
import json
import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import comments, config, domains, listicles, net, scoring, search, semrush

log = logging.getLogger(__name__)

PROSPECT_COLUMNS = [
    "page_url", "high_value", "comments_open", "dofollow", "authority", "edu_gov", "source_domain", "found_via",
    "spam_links", "dofollow_commenter_links", "comments_seen", "has_website_field", "platform", "open_evidence",
    "semrush_page_ascore", "listicle", "sample_commenter_link", "checked_at",
]


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _load_state():
    path = config.STATE_DIR / "seen_pages.json"
    if path.exists() and not config.RECRAWL:
        return set(json.loads(path.read_text()))
    return set()


def _save_state(seen):
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (config.STATE_DIR / "seen_pages.json").write_text(json.dumps(sorted(seen), indent=0))

DOMAIN_COLUMNS = ["domain", "score", "pages_seen", "on_seo_list_pages", "keyword_anchor_pages", "dofollow_pages",
                  "authority", "tranco_rank", "open_pagerank", "live", "example_pages"]


def _write(path, rows, columns):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _yn(v):
    return "yes" if v is True else "no" if v is False else "unknown"


def gather_targets(seen):
    lists = search.find_listicles()
    log.info("stage 1: %d listicles", len(lists))
    targets = {}

    def one(item):
        url, origin = item
        page = net.fetch(url)
        if not page.ok:
            return url, []
        if origin != "seeds.txt" and not listicles.looks_like_comment_list(page.final_url, page.html):
            log.info("  skipping off-topic search result %s", url)
            return url, None
        return url, listicles.extract_targets(page.final_url, page.html)

    listicle_pages = []
    with ThreadPoolExecutor(config.WORKERS) as ex:
        for listicle, found in ex.map(one, lists):
            if found is None:
                continue
            listicle_pages.append(listicle)
            log.info("  %s -> %d links", listicle, len(found))
            for u in found:
                targets.setdefault(u, listicle)

    # bare domains -> their most commented posts
    homepages = [u for u in targets if domains.is_homepage(u)]
    with ThreadPoolExecutor(config.WORKERS) as ex:
        for home, posts in zip(homepages, ex.map(listicles.posts_for_homepage, homepages)):
            src = targets.pop(home)
            for p in posts:
                targets.setdefault(p, src)

    # The 'dofollow list' posts are themselves full of comments from SEO link builders,
    # so they are checked first (always, even if seen before) as the best spammer source.
    lists_first = {u: u for u in listicle_pages}
    fresh = {u: s for u, s in targets.items() if u not in seen and u not in lists_first}
    log.info("stage 2: %d listicles + %d candidate comment pages (%d new)",
             len(lists_first), len(targets), len(fresh))
    picked = dict(lists_first)
    picked.update(list(fresh.items())[: max(0, config.MAX_TARGETS - len(picked))])
    return picked


_deadline = None


def _sample_link(r):
    """One commenter link and its rel, so a person can verify the dofollow call by eye."""
    links = sorted(r["links"], key=lambda l: (not l["dofollow"], l["kind"] != "author"))
    return f'{links[0]["url"]} (rel="{links[0]["rel"]}")' if links else ""


def check_all(urls):
    """Check pages in parallel, logging progress. Pages not started within
    CRAWL_MINUTES are skipped so a run always finishes and saves what it found."""
    global _deadline
    if _deadline is None:  # one budget for the whole run, shared by every check stage
        _deadline = time.time() + config.CRAWL_MINUTES * 60
    deadline = _deadline
    done = []

    def one(url):
        if time.time() > deadline:
            return None
        r = comments.check_page(url)
        done.append(1)
        if len(done) % 25 == 0:
            log.info("checked %d/%d pages", len(done), len(urls))
        return r

    with ThreadPoolExecutor(config.WORKERS) as ex:
        results = list(ex.map(one, urls))
    if len(done) < len(urls):
        log.warning("time limit: skipped %d pages", len(urls) - len(done))
    return results  # None where a page was skipped


def run():
    global _deadline
    _deadline = time.time() + config.CRAWL_MINUTES * 60  # whole-run budget for page checks
    t0 = time.time()
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    seen = _load_state()
    block = domains.blocklist()

    # stages 1-3
    targets = gather_targets(seen)
    results = [r for r in check_all(list(targets)) if r]
    log.info("stage 3: checked %d pages, %d open", len(results),
             sum(1 for r in results if r["comments_open"]))

    prospects, checked, link_rows = [], [], []
    stats = defaultdict(lambda: {"pages": set(), "dofollow_pages": set(), "listicle_pages": set(),
                                 "keyword_anchor_pages": set()})
    for r in results:
        page_domain = domains.root_domain(r["url"])
        row = {
            "page_url": r.get("final_url") or r["url"], "comments_open": _yn(r["comments_open"]),
            "dofollow": _yn(r["dofollow"]), "authority": scoring.authority(page_domain),
            "edu_gov": domains.tld_flag(r["url"]), "source_domain": domains.root_domain(targets[r["url"]]),
            "found_via": "blog commenting list", "dofollow_commenter_links": r.get("dofollow_links", 0),
            "comments_seen": r["comment_count"], "has_website_field": _yn(r["has_website_field"]),
            "platform": r["platform"], "open_evidence": r["open_evidence"] or r.get("error", ""),
            "semrush_page_ascore": "", "listicle": targets[r["url"]],
            "sample_commenter_link": _sample_link(r), "checked_at": _now(),
        }
        checked.append(row)
        row["spam_links"] = len(r["links"])
        link_rows += [{"page_url": row["page_url"], "page_authority": row["authority"], "edu_gov": row["edu_gov"],
                       "comments_open": row["comments_open"], **l} for l in r["links"]]
        if r["comments_open"] and r["links"]:  # open and already spammed (nofollow is fine)
            prospects.append(row)
        # stage 4: commenter domains (closed pages still tell us who the comment builders are)
        for l in r["links"]:
            d = l["domain"]
            if d == page_domain or domains.is_blocked(d, block):
                continue
            # A link inside the comment text is spam when it sits on a keyword ('Website
            # usability testing'); otherwise it is usually a reference (a news source).
            # Top-10k sites are never the link builders we are after.
            kw = scoring.is_keyword_anchor(l.get("anchor", ""), d, l["kind"])
            if (l["kind"] == "body" and not (l["dofollow"] or kw)) or 0 < scoring.tranco().get(d, 0) <= 10000:
                continue
            stats[d]["pages"].add(row["page_url"])
            if l["dofollow"]:
                stats[d]["dofollow_pages"].add(row["page_url"])
            if targets[r["url"]] == r["url"]:
                stats[d]["listicle_pages"].add(row["page_url"])
            if kw:
                stats[d]["keyword_anchor_pages"].add(row["page_url"])
        seen.add(r["url"])

    # Save the crawl now, so a later stage that stalls can't cost us these results.
    _write(config.OUT_DIR / "prospects.csv", prospects, PROSPECT_COLUMNS)
    _write(config.OUT_DIR / "all_checked_pages.csv", checked, PROSPECT_COLUMNS)
    # Every commenter link with its anchor, for a judgment pass on who is actually spamming.
    _write(config.OUT_DIR / "commenter_links.csv", link_rows,
           ["page_url", "page_authority", "edu_gov", "comments_open", "kind", "anchor", "url", "domain", "rel", "dofollow"])

    # stage 5
    scored = scoring.score_domains(stats) if stats else []
    top = [r for r in scored if r["live"]][: config.TOP_DOMAINS]
    log.info("stage 5: %d commenter domains, top %d: %s", len(scored), len(top),
             ", ".join(r["domain"] for r in top))
    semrush.write_queue(top, config.OUT_DIR / "semrush_queue.csv")
    _write(config.OUT_DIR / "commenter_domains.csv", scored, DOMAIN_COLUMNS)

    # stage 6
    units, missing = 0, []
    if top:
        found, units, missing = semrush.collect_candidates(top)
        req = config.OUT_DIR / "semrush_request.md"
        if missing:
            semrush.write_request(missing, top, req)
        elif req.exists():
            req.unlink()
        cand = []
        for d, rows in found.items():
            for c in semrush.filter_candidates(rows):
                cand.append((d, c))
        cand = [(d, c) for d, c in cand if c["source_url"] not in seen][: config.MAX_TARGETS]
        verified = check_all([c["source_url"] for _, c in cand])
        for (d, c), r in zip(cand, verified):
            if r is None:
                continue
            seen.add(c["source_url"])
            linked = any(l["domain"] == d for l in r["links"])
            row = {
                "page_url": c["source_url"], "comments_open": _yn(r["comments_open"]),
                "dofollow": _yn(r["dofollow"]),
                "authority": c["page_ascore"] or scoring.authority(domains.root_domain(c["source_url"])),
                "edu_gov": c["tld_flag"], "source_domain": d,
                "found_via": c["origin"] + (" (link confirmed on page)" if linked else ""),
                "dofollow_commenter_links": r.get("dofollow_links", 0), "comments_seen": r["comment_count"],
                "has_website_field": _yn(r["has_website_field"]), "platform": r["platform"],
                "open_evidence": r["open_evidence"] or r.get("error", ""),
                "semrush_page_ascore": c["page_ascore"] or "", "listicle": "", "checked_at": _now(),
            }
            checked.append(row)
            if r["comments_open"] and (linked or r["links"]):
                prospects.append(row)
    net.close_browser()

    # outputs
    # What Mash counts as worth building: dofollow on a .edu/.gov page, or on a DA 60+ site.
    for r in checked:
        r["high_value"] = "yes" if (
            r["edu_gov"] or int(r["authority"] or 0) >= config.HIGH_VALUE_AUTHORITY) else ""
    prospects.sort(key=lambda r: (r["high_value"] != "yes", r["edu_gov"] == "", r["dofollow"] != "yes",
                                  -int(r["authority"] or 0)))
    _write(config.OUT_DIR / "prospects.csv", prospects, PROSPECT_COLUMNS)
    _write(config.OUT_DIR / "all_checked_pages.csv", checked, PROSPECT_COLUMNS)
    _write(config.OUT_DIR / "commenter_domains.csv", scored, DOMAIN_COLUMNS)
    _save_state(seen)

    summary = {
        "finished_at": _now(), "minutes": round((time.time() - t0) / 60, 1),
        "pages_checked": len(checked), "prospects": len(prospects),
        "prospects_dofollow": sum(1 for p in prospects if p["dofollow"] == "yes"),
        "edu_gov": sum(1 for p in prospects if p["edu_gov"]),
        "high_value": sum(1 for p in prospects if p["high_value"]),
        "commenter_domains": len(scored), "semrush_domains": [r["domain"] for r in top],
        "semrush_mode": config.SEMRUSH_MODE, "semrush_units_used": units,
        "semrush_data_missing_for": missing,
    }
    (config.OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    log.info("done: %s", summary)
    return summary
