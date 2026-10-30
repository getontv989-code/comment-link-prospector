import functools
import http.server
import threading
from pathlib import Path

import pytest

from prospector import comments, config, listicles, net

FIX = Path(__file__).parent / "fixtures"


def page(name, url="https://example-blog.com/my-post/"):
    return net.Page(url, 200, (FIX / name).read_text())


def test_wordpress_open_dofollow():
    r = comments.analyze(page("wp_open_dofollow.html"))
    assert r["comments_open"] is True
    assert r["platform"] == "wordpress"
    assert r["has_website_field"]
    authors = [l for l in r["links"] if l["kind"] == "author"]
    assert len(authors) >= 30
    assert all(l["dofollow"] for l in authors)
    assert any(l["domain"] == "spammer-one.com" for l in authors)
    assert r["pagination"] == ["https://example-blog.com/my-post/comment-page-2/"]
    body = [l for l in r["links"] if l["kind"] == "body"]
    assert body and not any(l["dofollow"] for l in body)


def test_wordpress_nofollow():
    r = comments.analyze(page("wp_open_nofollow.html"))
    assert r["comments_open"] is True
    assert not any(l["dofollow"] for l in r["links"])


def test_meta_robots_nofollow_overrides_rel():
    r = comments.analyze(page("wp_meta_nofollow.html"))
    assert r["page_nofollow"]
    assert not any(l["dofollow"] for l in r["links"])


def test_wordpress_closed():
    r = comments.analyze(page("wp_closed.html"))
    assert r["comments_open"] is False
    assert "closed" in r["open_evidence"].lower()


def test_blogger():
    r = comments.analyze(page("blogger.html", "https://someblog.blogspot.com/2025/01/post.html"))
    assert r["comments_open"] is True
    assert r["platform"] == "blogger"
    assert len({l["domain"] for l in r["links"]}) == 4 and not any(l["dofollow"] for l in r["links"])


def test_listicle_extraction():
    html = (FIX / "listicle.html").read_text()
    urls = listicles.extract_targets("https://some-seo-list.in/list/", html)
    assert "https://example-blog.com/my-post/" in urls
    assert "https://another-blog.org/2024/05/some-article.html" in urls
    assert "https://www.homepage-only.com/" in urls
    assert "https://research.stanford.edu/blog/post-1" in urls
    assert not any("twitter.com" in u or "facebook.com" in u or "sidebar-junk" in u for u in urls)
    assert not any("some-seo-list.in" in u for u in urls)


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIX))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    net.close_browser()


def test_lazy_comments_need_scroll_and_click(server):
    """Comments and the form only appear after scrolling to the bottom of a ~3000
    paragraph page and pressing 'Load comments'. The static fetch sees nothing, so
    check_page must fall back to the headless browser."""
    static = comments.analyze(net.fetch(server + "/lazy.html"))
    assert static["comments_open"] is None and not static["links"]
    r = comments.check_page(server + "/lazy.html")
    assert r["rendered"]
    assert r["comments_open"] is True
    assert r["dofollow"] is True
    assert len({l["domain"] for l in r["links"]}) == 5


def test_check_page_closed_skips_browser(server, monkeypatch):
    called = []
    monkeypatch.setattr(net, "render", lambda *a, **k: called.append(1))
    r = comments.check_page(server + "/wp_closed.html")
    assert r["comments_open"] is False
    assert not called


def test_pipeline_end_to_end(tmp_path, monkeypatch):
    """Whole run on fixtures, with the network replaced by a fake web."""
    from prospector import pipeline, scoring, search

    wp = (FIX / "wp_open_dofollow.html").read_text()
    web = {
        "https://some-seo-list.in/list/": (FIX / "listicle.html").read_text(),
        "https://example-blog.com/my-post/": wp,
        "https://another-blog.org/2024/05/some-article.html": (FIX / "wp_closed.html").read_text(),
        "https://research.stanford.edu/blog/post-1": wp.replace("example-blog.com", "research.stanford.edu"),
        "https://www.homepage-only.com/feed/": (
            '<rss xmlns:slash="http://purl.org/rss/1.0/modules/slash/"><channel>'
            "<item><link>https://www.homepage-only.com/a-post/</link><slash:comments>12</slash:comments></item>"
            "<item><link>https://www.homepage-only.com/b-post/</link><slash:comments>90</slash:comments></item>"
            "</channel></rss>"),
        "https://www.homepage-only.com/b-post/": wp.replace("example-blog.com", "www.homepage-only.com"),
    }
    for d in ("spammer-one.com", "spammer-two.co.uk", "site9.com"):
        web[f"https://{d}"] = "<html>live</html>"

    def fake_fetch(url, timeout=None, **kw):
        html = web.get(url)
        return net.Page(url, 200 if html else 404, html or "", {}, url)

    monkeypatch.setattr(net, "fetch", fake_fetch)
    monkeypatch.setattr(net, "fetch_json", lambda *a, **k: None)
    monkeypatch.setattr(net, "render", lambda *a, **k: None)
    monkeypatch.setattr(search, "find_listicles", lambda: [("https://some-seo-list.in/list/", "seed")])
    monkeypatch.setattr(search, "search", lambda *a, **k: [])
    monkeypatch.setattr(scoring, "tranco", lambda: {"spammer-one.com": 50000, "facebook-like-giant.com": 50})
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "INPUTS_DIR", tmp_path / "inputs")
    (tmp_path / "inputs" / "semrush").mkdir(parents=True)
    # a manual Semrush export for the top spammer, as saved from the shared session
    (tmp_path / "inputs" / "semrush" / "export.csv").write_text(
        "Page ascore,Source title,Source url,Target url,Anchor,Nofollow\n"
        "72,Edu post,https://research.stanford.edu/blog/post-1,https://spammer-one.com/,x,false\n"
        "80,Big nofollow,https://bigsite.com/post/#comment-2,https://spammer-one.com/,x,true\n")

    summary = pipeline.run()
    import csv
    rows = list(csv.DictReader(open(tmp_path / "out" / "prospects.csv")))
    urls = [r["page_url"] for r in rows]
    assert "https://example-blog.com/my-post/" in urls
    assert "https://www.homepage-only.com/b-post/" in urls          # most-commented post from the feed
    assert not any("another-blog.org" in u for u in urls)            # comments closed
    assert rows[0]["edu_gov"] == ".edu"                              # .edu/.gov sorted first
    assert all(r["comments_open"] == "yes" for r in rows)
    assert rows[0]["comments_seen"] == "35"
    doms = list(csv.DictReader(open(tmp_path / "out" / "commenter_domains.csv")))
    assert doms[0]["domain"] == "spammer-one.com"
    assert "facebook.com" not in {d["domain"] for d in doms}
    assert summary["semrush_units_used"] == 0
    assert (tmp_path / "out" / "semrush_queue.csv").exists()
    # spammer-one.com came from the export, the other two need seoshope or the API
    assert "spammer-one.com" not in summary["semrush_data_missing_for"]
    assert "spammer-two.co.uk" in summary["semrush_data_missing_for"]
    assert "seoshope" in (tmp_path / "out" / "semrush_request.md").read_text()


def test_off_topic_search_results_are_skipped():
    assert listicles.looks_like_comment_list("https://x.in/list/", (FIX / "listicle.html").read_text())
    assert not listicles.looks_like_comment_list(
        "https://www.merriam-webster.com/dictionary/high", "<html><body>high: adjective, tall</body></html>")


def test_blogspot_blogs_are_separate_sites():
    from prospector import domains
    html = '<article><div class="post-body">' + "blog commenting dofollow list " * 40 + \
           '<a href="https://other-blog.blogspot.com/2024/01/post.html">x</a></div></article>'
    assert listicles.extract_targets("https://list-site.blogspot.com/2025/10/list.html", html) == \
        ["https://other-blog.blogspot.com/2024/01/post.html"]
    assert domains.root_domain("https://a.blogspot.com/") != domains.root_domain("https://b.blogspot.com/")


def test_browser_can_be_used_from_worker_threads(server):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as ex:
        results = list(ex.map(net.render, [server + "/lazy.html"] * 2))
    assert all(r and r.rendered for r in results)


def test_search_relevance_rejects_wrong_query_results():
    from prospector import search
    good = [("https://x.in/blog-commenting-sites", "100 Dofollow Blog Commenting Sites List 2025"),
            ("https://y.com/a", "High DA blog commenting sites")]
    bad = [("https://support.microsoft.com/x", "Fix Bluetooth audio devices"),
           ("https://www.merriam-webster.com/dictionary/high", "High Definition & Meaning")]
    assert search.relevant("dofollow blog commenting list 2025", good)
    assert not search.relevant("dofollow blog commenting list 2025", bad)


def test_most_commented_posts_from_recent_comments(monkeypatch):
    data = [{"link": "https://b.com/post-a/#comment-1"}, {"link": "https://b.com/post-b/comment-page-2/#comment-9"},
            {"link": "https://b.com/post-b/#comment-3"}, {"link": "https://b.com/post-b/#comment-4"}]
    monkeypatch.setattr(net, "fetch_json", lambda url, **k: data if "/wp-json/wp/v2/comments" in url else None)
    monkeypatch.setattr(net, "fetch", lambda url, **k: net.Page(url, 404, ""))
    assert listicles.posts_for_homepage("https://b.com/", limit=1) == ["https://b.com/post-b/"]


def test_fetch_gives_up_on_endless_stream(monkeypatch):
    """A homepage that streams forever (internet radio and the like) must not hang the run."""
    import time

    class Endless(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"<p>x</p>" * 100)
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Endless)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    t0 = time.time()
    page = net.fetch(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=1)
    srv.shutdown()
    assert page.status == 200 and page.html
    assert time.time() - t0 < 10


def test_blogger_comment_feed_gives_commenters(monkeypatch):
    html = ('<html><head><link rel="alternate" href="https://list.blogspot.com/feeds/123/comments/default">'
            '</head><body><div id="comment-editor"></div></body></html>')
    feed = {"feed": {"entry": [
        {"author": [{"name": {"$t": "best dentist in pune"}, "uri": {"$t": "https://spam-dental.in/"}}]},
        {"author": [{"name": {"$t": "Jo"}, "uri": {"$t": "https://www.blogger.com/profile/1"}}]},
    ]}}
    monkeypatch.setattr(net, "fetch", lambda url, **k: net.Page(url, 200, html))
    monkeypatch.setattr(net, "fetch_json", lambda url, **k: feed if "/feeds/123/comments" in url else None)
    monkeypatch.setattr(net, "render", lambda *a, **k: None)
    r = comments.check_page("https://list.blogspot.com/2025/10/list.html")
    assert [l["domain"] for l in r["links"]] == ["spam-dental.in"]
    assert r["comment_count"] == 2 and r["dofollow"] is None
