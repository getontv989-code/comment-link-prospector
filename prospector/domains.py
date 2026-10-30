"""Root-domain handling and the platform blocklist."""
from urllib.parse import urlsplit, urlunsplit

import tldextract

from . import config

_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)  # bundled PSL, no network

# Social networks, big platforms, link shorteners and CDNs. Spammers link these but
# they tell us nothing about who is building comment links.
DEFAULT_BLOCK = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com", "pinterest.com",
    "youtube.com", "youtu.be", "tiktok.com", "reddit.com", "quora.com", "medium.com",
    "tumblr.com", "wordpress.com", "wordpress.org", "blogger.com", "blogspot.com", "google.com",
    "goo.gl", "bit.ly", "t.co", "tinyurl.com", "ow.ly", "amazon.com", "amzn.to", "wikipedia.org",
    "github.com", "apple.com", "microsoft.com", "gravatar.com", "wp.com", "disqus.com", "disq.us",
    "gstatic.com", "googleusercontent.com", "cloudflare.com", "yahoo.com", "bing.com", "whatsapp.com",
    "telegram.org", "t.me", "wa.me", "gmail.com", "feedburner.com", "akismet.com", "jetpack.com",
    "w.org", "schema.org", "creativecommons.org", "addtoany.com", "sharethis.com", "vk.com",
    "flickr.com", "vimeo.com", "soundcloud.com", "spotify.com", "issuu.com", "slideshare.net",
    "scribd.com", "about.me", "wix.com", "weebly.com", "substack.com", "linktr.ee", "hubspot.com",
    "shopify.com", "godaddy.com", "yelp.com", "trustpilot.com", "mozilla.org", "w3.org",
}


def blocklist():
    return DEFAULT_BLOCK | {d.lower() for d in config.read_list("blocklist.txt")}


def root_domain(url_or_host):
    s = url_or_host.strip()
    if "//" not in s:
        s = "http://" + s
    ext = _extract(s)
    if not ext.domain or not ext.suffix:
        return ""
    return f"{ext.domain}.{ext.suffix}".lower()


def host(url):
    return (urlsplit(url).hostname or "").lower()


def is_blocked(domain, block=None):
    block = block if block is not None else blocklist()
    return domain in block


def tld_flag(url_or_domain):
    """'.gov' / '.edu' (including country forms like .gov.uk, .edu.au, .ac.uk) or ''."""
    ext = _extract(url_or_domain if "//" in url_or_domain else "http://" + url_or_domain)
    parts = ext.suffix.split(".") if ext.suffix else []
    if "gov" in parts or "mil" in parts:
        return ".gov"
    if "edu" in parts or (len(parts) == 2 and parts[0] == "ac"):
        return ".edu"
    return ""


def normalize_url(url):
    """Drop fragments and tracking query strings so the same page is not crawled twice."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return ""
    query = "&".join(q for q in parts.query.split("&")
                     if q and not q.lower().startswith(("utm_", "fbclid", "gclid", "replytocom")))
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", query, ""))


def is_homepage(url):
    p = urlsplit(url)
    return p.path in ("", "/") and not p.query
