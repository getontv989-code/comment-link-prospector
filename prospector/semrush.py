"""Stage 6: for the top few commenter domains, find their high-authority dofollow
blog-comment backlinks, flag .edu/.gov, and verify each one live.

Backlink sources, cheapest first:
  1. Semrush exports from the shared seoshope.com session, uploaded to inputs/semrush/*.csv.
     Costs no API units.
  2. Semrush API, only when SEMRUSH_MODE=api AND SEMRUSH_UNIT_BUDGET is set, and only for
     domains option 1 did not cover. The job stops before it would exceed the budget.
  3. Free footprint search ('"domain.com" "leave a reply"') for anything still uncovered.
     Those domains are also written to semrush_request.md, which the workflow posts as a
     GitHub issue asking for option 1 or 2.
Every candidate is then fetched and checked with the same comment checker as stage 3,
so a row only survives if comments are open and the domain's link is really there."""
import csv
import logging
import re
from urllib.parse import quote

from . import config, domains, net, search

log = logging.getLogger(__name__)

UNITS_PER_BACKLINK_ROW = 40   # Semrush API price for the 'backlinks' report
COMMENT_URL_HINT = re.compile(r"comment|#respond|replytocom|cpage=|/\d{4}/\d{2}/|\.html", re.I)


def ui_link(domain):
    return (f"{config.SEMRUSH_UI_HOST}/analytics/backlinks/backlinks/?q={quote(domain)}"
            f"&searchType=domain")


def write_queue(top, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["domain", "score", "pages_seen", "authority", "semrush_backlinks_url",
                    "export_to (save the Semrush CSV here, any filename)"])
        for r in top:
            w.writerow([r["domain"], r["score"], r["pages_seen"], r["authority"], ui_link(r["domain"]),
                        "inputs/semrush/"])


def _norm_header(h):
    return re.sub(r"[^a-z]", "", h.lower())


def read_manual_exports():
    """Rows from any Semrush backlinks CSV export the user saved in inputs/semrush/."""
    rows = []
    for path in sorted((config.INPUTS_DIR / "semrush").glob("*.csv")):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
            for raw in csv.DictReader(f, dialect=dialect):
                r = {_norm_header(k or ""): (v or "").strip() for k, v in raw.items()}
                src = r.get("sourceurl") or r.get("source") or ""
                if not src.startswith("http"):
                    continue
                rows.append({
                    "source_url": src,
                    "target_url": r.get("targeturl", ""),
                    "page_ascore": _num(r.get("pageascore") or r.get("pageas") or r.get("ascore")),
                    "nofollow": (r.get("nofollow", "").lower() in ("true", "1", "yes")),
                    "origin": f"manual export {path.name}",
                })
    return rows


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


_dead_keys = set()


def _api_keys():
    """SEMRUSH_API_KEY may hold several keys, comma separated, one per account you pay for.
    A key whose units run out is skipped for the rest of the run."""
    return [k.strip() for k in config.SEMRUSH_API_KEY.split(",") if k.strip() and k.strip() not in _dead_keys]


def api_backlinks(domain, budget_left):
    rows_wanted = min(config.SEMRUSH_ROWS, budget_left // UNITS_PER_BACKLINK_ROW)
    if rows_wanted <= 0:
        return [], 0
    text = ""
    for key in _api_keys():
        params = {
            "key": key, "type": "backlinks", "target": domain,
            "target_type": "root_domain", "display_limit": rows_wanted,
            "export_columns": "page_ascore,source_title,source_url,target_url,anchor,nofollow,form,frame",
            "display_sort": "page_ascore_desc",  # best pages first; nofollow kept on purpose
        }
        r = net.session().get(config.SEMRUSH_DATABASE_HOST + "/analytics/v1/", params=params, timeout=60)
        text = r.text.strip()
        if text.startswith("ERROR") and ("BALANCE" in text.upper() or "LIMIT" in text.upper()
                                         or "WRONG KEY" in text.upper()):
            log.warning("Semrush key ...%s is out (%s); trying the next one", key[-4:], text[:60])
            _dead_keys.add(key)
            continue
        break
    if not text or text.startswith("ERROR"):
        log.warning("Semrush API error for %s: %s", domain, text[:120] or "no usable key")
        return [], 0
    lines = text.splitlines()
    out = []
    for raw in csv.DictReader(lines, delimiter=";"):
        r2 = {_norm_header(k): v for k, v in raw.items()}
        out.append({"source_url": r2.get("sourceurl", ""), "target_url": r2.get("targeturl", ""),
                    "page_ascore": _num(r2.get("pageascore")),
                    "nofollow": r2.get("nofollow", "").lower() == "true", "origin": "semrush api"})
    return out, len(out) * UNITS_PER_BACKLINK_ROW


def footprint_search(domain, limit=10):
    """Free: pages that mention the domain next to a comment footprint."""
    out = []
    for q in (f'"{domain}" "leave a reply"', f'"{domain}" "post a comment"',
              f'"{domain}" inurl:comment-page'):
        for u in search.search(q, limit):
            if domains.root_domain(u) != domain:
                out.append({"source_url": u, "target_url": "", "page_ascore": 0, "nofollow": False,
                            "origin": "footprint search"})
    return out


def collect_candidates(top):
    """{domain: [backlink rows]} using the cheapest sources first."""
    names = {r["domain"] for r in top}
    found = {d: [] for d in names}
    units_used = 0

    for row in read_manual_exports():
        d = domains.root_domain(row["target_url"]) if row["target_url"] else ""
        if d in found:
            found[d].append(row)
        elif len(names) == 1:
            found[next(iter(names))].append(row)
    have_export = {d for d in names if found[d]}

    # Option 2: the Semrush API, only for domains the seoshope exports did not cover.
    if config.SEMRUSH_MODE == "api":
        if not config.SEMRUSH_API_KEY or config.SEMRUSH_UNIT_BUDGET <= 0:
            log.warning("SEMRUSH_MODE=api but no SEMRUSH_API_KEY / SEMRUSH_UNIT_BUDGET; skipping API")
        else:
            for d in sorted(names - have_export):
                left = config.SEMRUSH_UNIT_BUDGET - units_used
                rows, cost = api_backlinks(d, left)
                units_used += cost
                found[d] += rows
                log.info("Semrush API %s: %d rows, %d units (total %d/%d)", d, len(rows), cost,
                         units_used, config.SEMRUSH_UNIT_BUDGET)

    missing = sorted(d for d in names if not found[d])
    for d in names if config.FOOTPRINT else []:
        if not found[d]:
            found[d] += footprint_search(d)
    return found, units_used, missing


def write_request(missing, top, path):
    """Asks the user for Semrush data: an export from the shared seoshope session, or an
    OK to use API credits. The workflow posts this as a GitHub issue."""
    by = {r["domain"]: r for r in top}
    lines = [
        "The prospector found these comment-link builders but has no Semrush backlink data for them yet. "
        "This run used free footprint search instead.",
        "",
        "| Domain | Comment pages seen | Semrush backlinks |",
        "| --- | --- | --- |",
    ]
    for d in missing:
        lines.append(f"| {d} | {by[d]['pages_seen']} | [open]({ui_link(d)}) |")
    lines += [
        "",
        "Pick either option. The next run picks it up automatically.",
        "",
        "1. **Shared Semrush (seoshope.com), 0 credits.** Log in, open each link above "
        "(swap the host for the seoshope server if needed), export Backlinks as CSV, and upload the files to "
        "`inputs/semrush/` in this repo (Add file > Upload files). Uploading starts a run. "
        "If none of the seoshope servers work, get a replacement login or use option 2.",
        "2. **Semrush API credits.** In Settings > Secrets and variables > Actions, add the secret "
        "`SEMRUSH_API_KEY` and the variables `SEMRUSH_MODE` = `api` and `SEMRUSH_UNIT_BUDGET` "
        f"(each backlink row costs {UNITS_PER_BACKLINK_ROW} units; `6000` covers about "
        f"{6000 // UNITS_PER_BACKLINK_ROW} rows a run). The API is only used for domains without an export.",
    ]
    path.write_text("\n".join(lines) + "\n")


def filter_candidates(rows):
    """Keep dofollow, likely-comment pages above the authority floor (.edu/.gov always kept)."""
    keep, seen = [], set()
    for r in rows:
        u = domains.normalize_url(r["source_url"])
        if not u or u in seen:  # nofollow is fine: Mash builds these for authority, not just link juice
            continue
        flag = domains.tld_flag(u)
        if r["page_ascore"] and r["page_ascore"] < config.MIN_AUTHORITY and not flag:
            continue
        if r["origin"].startswith(("semrush", "manual")) and not COMMENT_URL_HINT.search(r["source_url"]) \
                and not flag:
            continue
        seen.add(u)
        keep.append({**r, "source_url": u, "tld_flag": flag})
    keep.sort(key=lambda r: (r["tld_flag"] == "", -r["page_ascore"]))
    return keep
