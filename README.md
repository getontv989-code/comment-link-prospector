# Comment link prospector

Finds blog posts that still accept comments and give dofollow links, by following the
people who already build comment links. It only collects URLs. It never posts, comments
or submits a form anywhere.

It runs by itself every day on GitHub Actions. Nobody has to click anything.

## What one run does

1. **Find lists.** Searches the queries in `config/queries.txt` ("dofollow blog commenting list 2025",
   ".gov dofollow blog commenting list", ...) and adds the listicles in `config/seeds.txt`.
2. **Pull the comment pages out of each list**, including URLs written as plain text and
   bare domains. For a bare domain it reads the site's RSS feed and picks the posts with
   the most comments.
3. **Check each page.** Comments open (comment form present, or WordPress's own API says
   `comment_status: open`), or closed ("Comments are closed"). The whole HTML is read, so a
   form at the bottom of a page with thousands of comments is found without scrolling. If
   the comments or the form are loaded by JavaScript, a headless Chrome scrolls to the very
   bottom until the page stops growing and presses "Load more comments" / "Leave a comment"
   buttons (never submit buttons). Older comment pages (`/comment-page-2/`) are followed too.
   Commenter links are dofollow unless their `rel` has nofollow, ugc or sponsored, or the
   page has a `robots nofollow` meta tag.
4. **Collect the commenters' sites** from 30-50 comments per page, reduce to root domains,
   drop social networks and big platforms (`prospector/domains.py`, plus `config/blocklist.txt`).
5. **Score them for free**: how many comment pages each domain appears on, how many of those
   were dofollow, Tranco rank / Open PageRank, and whether the site is live. Only the top
   `TOP_DOMAINS` (default 5) go on to step 6.
6. **Find their high-authority comment backlinks**, cheapest source first: Semrush exports you
   drop into `inputs/semrush/`, then a free footprint search, then the Semrush API only if you
   turn it on with a unit budget. Every candidate is re-checked live: comments open, dofollow,
   and the commenter's link actually on the page. `.edu` and `.gov` (and `.ac.uk`, `.gov.uk`, ...)
   are flagged and sorted to the top.

## Output

Each run commits to `results/` and attaches the same files to the Actions run:

| File | What's in it |
| --- | --- |
| `prospects.csv` | The list for the data-entry team: open pages only. `page_url, comments_open, dofollow, authority, edu_gov, source_domain, found_via, ...` |
| `prospects-YYYY-MM-DD.csv` | That day's copy |
| `all_checked_pages.csv` | Every page checked, including closed ones and why |
| `commenter_domains.csv` | Every comment-building domain found, with its free score |
| `semrush_queue.csv` | The top domains, with a direct Semrush backlinks link for each |
| `summary.json` | Counts, and Semrush units used (0 unless the API is turned on) |

The CSVs open directly in Google Sheets (File > Import). `authority` is a free 0-100
estimate (Open PageRank, or Tranco rank) unless Semrush gave a Page Authority Score, which
is in `semrush_page_ascore`.

`state/seen_pages.json` remembers what has been checked, so each day's list is new pages.

## Semrush without spending credits

- Default (`SEMRUSH_MODE=off`): no API calls. Open the links in `results/semrush_queue.csv` in
  any logged-in Semrush (the shared seoshope.com session works), export Backlinks as CSV, and
  commit the file into `inputs/semrush/`. That starts a run which verifies those backlinks.
- API (`SEMRUSH_MODE=api`): set repo variables `SEMRUSH_MODE=api` and `SEMRUSH_UNIT_BUDGET`
  (for example `6000`) plus the `SEMRUSH_API_KEY` secret. One backlink row costs 40 units, so
  the budget caps it at `budget / 40` rows per run, top domains only. Without a budget it
  refuses to call the API.

## Settings (repo variables or secrets, all optional)

`MAX_TARGETS` (400), `TOP_DOMAINS` (5), `MIN_AUTHORITY` (30), `SEMRUSH_MODE`, `SEMRUSH_UNIT_BUDGET`,
secrets `SEMRUSH_API_KEY`, `OPENPAGERANK_API_KEY` (free, better authority numbers),
`SERPER_API_KEY` / `BRAVE_API_KEY` (more reliable search than keyless DuckDuckGo/Bing scraping).

## Running locally

```
pip install -r requirements.txt && python -m playwright install chromium
python -m prospector                      # full run, writes out/
python -m prospector --check URL [URL...] # check single pages
python -m pytest -q tests
```
