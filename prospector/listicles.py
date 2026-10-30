"""Stage 2: pull the comment-page URLs out of a listicle, and turn bare domains
(many lists only give 'example.com') into real posts that have comments."""
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import config, domains, net

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
CONTENT_SELECTORS = [".entry-content", ".post-body", "article", ".post-content", ".article-content",
                     "main", "#content", ".content"]
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|pdf|zip|mp4|mp3|css|js)(\?|$)", re.I)


def _content_root(soup):
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 500:
            return el
    return soup.body or soup


LIST_TOPIC_RE = re.compile(r"comment", re.I)
LIST_SEO_RE = re.compile(r"do-?follow|backlinks?|high\s*(da|pr)|\.edu|\.gov|seo", re.I)


def looks_like_comment_list(url, html):
    """Search engines (Bing especially) sometimes return unrelated pages; only keep pages
    that are actually about blog commenting for SEO."""
    text = BeautifulSoup(html, "lxml").get_text(" ")[:200000]
    head = url + " " + text[:3000]
    return bool(LIST_TOPIC_RE.search(head) and LIST_SEO_RE.search(text))


def extract_targets(listicle_url, html):
    """Every external URL in the list body, as written (plain-text URLs included)."""
    soup = BeautifulSoup(html, "lxml")
    for bad in soup.select("nav, header, footer, aside, .sidebar, .widget, #comments, .comments, "
                           ".comment-list, .sharedaddy, .share, .related, script, style"):
        bad.decompose()
    root = _content_root(soup)
    own = domains.root_domain(listicle_url)
    block = domains.blocklist()
    found = []
    for a in root.find_all("a", href=True):
        found.append(urljoin(listicle_url, a["href"]))
    found += URL_RE.findall(root.get_text(" "))
    # lists that write bare domains like "bloggerspassion.com" in a table
    for cell in root.find_all(["td", "li"]):
        t = cell.get_text(" ", strip=True)
        if re.fullmatch(r"(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?", t, re.I):
            found.append("https://" + t)
    out, seen = [], set()
    for u in found:
        u = domains.normalize_url(u.rstrip(".,;"))
        if not u or SKIP_EXT.search(u):
            continue
        d = domains.root_domain(u)
        if not d or d == own or (domains.is_blocked(d, block) and not domains.tld_flag(u)):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def posts_for_homepage(url, limit=None):
    """For a bare domain, use its RSS feed to pick the posts with the most comments.
    WordPress feeds carry <slash:comments>, Blogger feeds carry <thr:total>."""
    limit = limit or config.POSTS_PER_HOMEPAGE
    base = url.rstrip("/")
    candidates = []
    for feed in (base + "/feed/", base + "/feeds/posts/default?alt=rss&max-results=50", base + "/rss.xml"):
        page = net.fetch(feed)
        if not page.ok or "<item" not in page.html:
            continue
        soup = BeautifulSoup(page.html, "xml")
        for item in soup.find_all("item"):
            link = item.find("link")
            count = item.find(["slash:comments", "comments", "thr:total"]) or item.find("total")
            try:
                n = int(count.get_text(strip=True)) if count and count.get_text(strip=True).isdigit() else 0
            except ValueError:
                n = 0
            if link and link.get_text(strip=True):
                candidates.append((n, link.get_text(strip=True)))
        if candidates:
            break
    if not candidates:  # no feed: take article links from the homepage itself
        page = net.fetch(url)
        if page.ok:
            soup = BeautifulSoup(page.html, "lxml")
            own = domains.root_domain(url)
            for a in soup.select("article a[href], h2 a[href], h3 a[href], .entry-title a[href]"):
                u = urljoin(url, a["href"])
                if domains.root_domain(u) == own and not domains.is_homepage(u):
                    candidates.append((0, u))
    candidates.sort(key=lambda c: -c[0])
    seen, out = set(), []
    for _, u in candidates:
        u = domains.normalize_url(u)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= limit:
            break
    return out
