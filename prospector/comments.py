"""Stages 3 and 4: decide whether a page still accepts comments, whether commenter links
are dofollow, and collect the commenters' URLs.

The full HTML of a page contains every comment and the comment form no matter how far
down they sit, so a plain fetch already 'reaches the bottom' of a long page. A headless
browser is only used when the comments or the form are injected by JavaScript (lazy
loading, 'load more' buttons, wpDiscuz, Disqus, Blogger iframes)."""
import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from . import config, domains, net

log = logging.getLogger(__name__)

FORM_SELECTORS = [
    ("wordpress", "form#commentform"),
    ("wordpress", "form[action*='wp-comments-post']"),
    ("wordpress", "#respond textarea"),
    ("wpdiscuz", "#wpdcom .wpd-form, #wpdcom textarea, .wpd-form-wrap textarea"),
    ("blogger", "#comment-editor, iframe[src*='blogger.com/comment'], #comment-post-message, "
                "a[href*='blogger.com/comment'], a[href*='/comment/fullpage/']"),
    ("generic", "form textarea[name='comment'], textarea#comment, #comment-form textarea, "
                ".comment-form textarea, form[id*='comment'] textarea, form[class*='comment'] textarea, "
                "textarea[name*='comment']"),
]
DISQUS_SELECTOR = "#disqus_thread, iframe[src*='disqus.com']"
CLOSED_RE = re.compile(
    r"comments?\s+(are|is)\s+(now\s+)?closed|commenting\s+(is\s+)?(closed|disabled)|"
    r"comments?\s+(for|on)\s+this\s+(post|article|entry)\s+(are|is)\s+closed|"
    r"comments?\s+(have\s+been|are)\s+(disabled|turned\s+off)|comments?\s+closed", re.I)
CLOSED_SELECTORS = ".nocomments, .comments-closed, p.no-comments, .comments-are-closed"

AUTHOR_LINK_SELECTORS = (
    ".comment-author a[href], cite.fn a[href], b.fn a[href], .fn a.url[href], a.url[href], "
    ".comment-author-link a[href], .comment-meta .author a[href], .wpd-comment-author a[href], "
    "cite.user a[href], .comment-header .user a[href], .commenter a[href], .comment-author-name a[href], "
    ".comment-name a[href], .says a[href]"
)
BODY_LINK_SELECTORS = (
    ".comment-content a[href], .comment-body p a[href], .comment-text a[href], "
    ".wpd-comment-text a[href], .comment-content p a[href], p.comment-content a[href]"
)
COMMENT_ITEM_RE = re.compile(r"^(comment|wpd-comm|c)[-_]?\d+$", re.I)
PAGINATION_RE = re.compile(r"/comment-page-\d+|[?&]cpage=\d+|[?&]commentPage=\d+", re.I)
LAZY_HINTS = re.compile(r"wpdiscuz|disqus|lazy-?load-?comments|load-?more-?comments|"
                        r"comments-?lazy|data-src=[\"'][^\"']*comment|jetpack_remote_comment|"
                        r"blogger\.com/comment|commentsLoaded|facebook\.com/plugins/comments", re.I)
NOFOLLOW_RELS = {"nofollow", "ugc", "sponsored"}


def _rel(a):
    rel = a.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    return {r.lower() for r in rel}


def _meta_nofollow(soup, headers):
    for m in soup.find_all("meta", attrs={"name": re.compile(r"^(robots|googlebot)$", re.I)}):
        if "nofollow" in (m.get("content") or "").lower():
            return True
    return "nofollow" in (headers.get("X-Robots-Tag") or headers.get("x-robots-tag") or "").lower()


def analyze(page):
    """Inspect one HTML page. Returns a dict; comments_open is True, False or None (unknown)."""
    soup = BeautifulSoup(page.html, "lxml")
    base = page.final_url
    res = {
        "platform": "", "comments_open": None, "open_evidence": "", "has_website_field": False,
        "page_nofollow": _meta_nofollow(soup, page.headers), "links": [], "comment_count": 0,
        "pagination": [], "lazy_hint": bool(LAZY_HINTS.search(page.html)),
        "wp_json": "", "rendered": page.rendered,
    }

    if soup.find("link", rel="https://api.w.org/") or "/wp-content/" in page.html:
        res["platform"] = "wordpress"
    alt = soup.find("link", attrs={"rel": "alternate", "type": "application/json"})
    if alt and "/wp-json/" in (alt.get("href") or ""):
        res["wp_json"] = urljoin(base, alt["href"])
    if "blogger.com" in page.html and ("blogspot" in base or "b:skin" in page.html or "BLOG_" in page.html):
        res["platform"] = res["platform"] or "blogger"

    for platform, sel in FORM_SELECTORS:
        el = soup.select_one(sel)
        if el is not None:
            res["comments_open"] = True
            res["open_evidence"] = f"comment form found ({sel.split(',')[0].strip()})"
            if platform != "generic" or not res["platform"]:
                res["platform"] = platform if platform != "generic" else (res["platform"] or "generic")
            break
    if soup.select_one(DISQUS_SELECTOR):
        res["platform"] = "disqus"
        if res["comments_open"] is None:
            res["comments_open"] = True
            res["open_evidence"] = "Disqus thread present"
    if soup.select_one("form input[name='url'], form input#url, form input[name='website']"):
        res["has_website_field"] = True

    if res["comments_open"] is None:
        closed_el = soup.select_one(CLOSED_SELECTORS)
        scope = soup.select_one("#comments, .comments-area, #respond, .comments") or soup
        m = CLOSED_RE.search(scope.get_text(" ", strip=True))
        if closed_el is not None or m:
            res["comments_open"] = False
            res["open_evidence"] = "closed notice: " + (m.group(0) if m else closed_el.get_text(" ", strip=True)[:60])

    # commenter links
    own = domains.root_domain(base)
    seen = set()
    for sel, kind in ((AUTHOR_LINK_SELECTORS, "author"), (BODY_LINK_SELECTORS, "body")):
        for a in soup.select(sel):
            href = urljoin(base, a.get("href", ""))
            if not href.startswith("http"):
                continue
            d = domains.root_domain(href)
            if not d or d == own:
                continue
            key = (href, kind)
            if key in seen:
                continue
            seen.add(key)
            rel = _rel(a)
            dofollow = not (rel & NOFOLLOW_RELS) and not res["page_nofollow"]
            if "disq.us" in href or "/goto/" in href or "/out/" in href:
                dofollow = False  # redirect wrapper, link equity does not pass to the target
            res["links"].append({"url": href, "domain": d, "kind": kind, "anchor": a.get_text(" ", strip=True)[:120],
                                 "rel": " ".join(sorted(rel)), "dofollow": dofollow})

    ids = {el.get("id") for el in soup.find_all(id=COMMENT_ITEM_RE)}
    items = len(ids) or len(soup.select("li.comment, div.comment, article.comment, .wpd-comment"))
    res["comment_count"] = items

    path = urlsplit(base).path.split("/comment-page-")[0].rstrip("/")
    for a in soup.find_all("a", href=PAGINATION_RE):
        u = urljoin(base, a["href"]).split("#")[0]
        if urlsplit(u).path.split("/comment-page-")[0].rstrip("/") == path:
            res["pagination"].append(u)
    res["pagination"] = sorted(set(res["pagination"]))
    return res


def _wp_comment_status(url, info):
    """WordPress tells us directly whether comments are open on a post."""
    api = info.get("wp_json")
    if not api:
        slug = [p for p in urlsplit(url).path.split("/") if p and not p.startswith("comment-page-")]
        if not slug:
            return None
        api = "{0.scheme}://{0.netloc}/wp-json/wp/v2/posts?slug={1}".format(urlsplit(url), slug[-1])
    data = net.fetch_json(api + ("&" if "?" in api else "?") + "_fields=comment_status")
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict) and data.get("comment_status") in ("open", "closed"):
        return data["comment_status"] == "open"
    return None


def check_page(url):
    """Full check of one candidate comment page. Never posts anything."""
    page = net.fetch(url)
    if not page.ok:
        return {"url": url, "status": page.status, "error": "fetch failed", "comments_open": None,
                "links": [], "comment_count": 0, "platform": "", "open_evidence": "",
                "dofollow": None, "rendered": False, "has_website_field": False}
    info = analyze(page)

    if info["platform"] == "wordpress":
        wp = _wp_comment_status(page.final_url, info)
        if wp is not None:
            if info["comments_open"] is None or info["comments_open"] != wp:
                info["open_evidence"] = f"WordPress API comment_status={'open' if wp else 'closed'}"
            info["comments_open"] = wp

    # Fall back to a real browser when the static HTML is inconclusive or comments are lazy loaded.
    needs_browser = (info["comments_open"] is None
                     or (info["lazy_hint"] and len(info["links"]) < config.MIN_COMMENTS)
                     or (info["comments_open"] and info["platform"] in ("disqus", "wpdiscuz", "blogger")
                         and not info["links"]))
    if needs_browser:
        rendered = net.render(page.final_url)
        if rendered and rendered.ok:
            r = analyze(rendered)
            if r["comments_open"] is not None or len(r["links"]) > len(info["links"]):
                keep_open = info["comments_open"] if info["open_evidence"].startswith("WordPress API") else None
                info.update({k: r[k] for k in ("links", "comment_count", "pagination", "has_website_field")})
                if r["comments_open"] is not None and keep_open is None:
                    info["comments_open"] = r["comments_open"]
                    info["open_evidence"] = r["open_evidence"] + " (rendered)"
                if r["platform"]:
                    info["platform"] = info["platform"] or r["platform"]
            info["rendered"] = True

    # Old comment pages (comment-page-N) hold most of the commenters on long threads.
    visited = {page.final_url}
    queue = [u for u in info["pagination"] if u not in visited]
    while queue and len(info["links"]) < config.MAX_COMMENTS and len(visited) <= config.MAX_COMMENT_PAGES:
        nxt = queue.pop()
        visited.add(nxt)
        p = net.fetch(nxt)
        if not p.ok:
            continue
        extra = analyze(p)
        have = {(l["url"], l["kind"]) for l in info["links"]}
        info["links"] += [l for l in extra["links"] if (l["url"], l["kind"]) not in have]
        info["comment_count"] += extra["comment_count"]
        queue += [u for u in extra["pagination"] if u not in visited and u not in queue]

    info["links"] = info["links"][: config.MAX_COMMENTS * 2]
    author = [l for l in info["links"] if l["kind"] == "author"]
    pool = author or info["links"]
    info["dofollow"] = (any(l["dofollow"] for l in pool) if pool else None)
    info["dofollow_links"] = sum(1 for l in pool if l["dofollow"])
    info["url"] = url
    info["final_url"] = page.final_url
    info["status"] = page.status
    info["error"] = ""
    return info
