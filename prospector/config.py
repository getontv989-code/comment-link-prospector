"""Runtime settings. Everything can be overridden with environment variables so the
GitHub Actions job never needs interactive input."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
OUT_DIR = Path(os.environ.get("OUT_DIR", ROOT / "out"))
STATE_DIR = Path(os.environ.get("STATE_DIR", ROOT / "state"))
INPUTS_DIR = ROOT / "inputs"


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
)
TIMEOUT = _int("HTTP_TIMEOUT", 20)
WORKERS = _int("WORKERS", 8)

# Crawl caps keep one run inside the GitHub Actions 6h limit.
MAX_SEARCH_RESULTS = _int("MAX_SEARCH_RESULTS", 10)   # per query
MAX_LISTICLES = _int("MAX_LISTICLES", 40)
MAX_TARGETS = _int("MAX_TARGETS", 400)                # comment pages to check
POSTS_PER_HOMEPAGE = _int("POSTS_PER_HOMEPAGE", 2)    # when a list only names a domain
MIN_COMMENTS = _int("MIN_COMMENTS", 30)
MAX_COMMENTS = _int("MAX_COMMENTS", 50)
MAX_COMMENT_PAGES = _int("MAX_COMMENT_PAGES", 5)      # comment-page-N pagination depth
USE_BROWSER = _bool("USE_BROWSER", True)              # headless fallback for lazy/JS comments
BROWSERS = _int("BROWSERS", 3)                        # headless Chromium instances in parallel
RECRAWL = _bool("RECRAWL", False)                     # ignore state/seen_pages.json

# Pre-scoring and Semrush
TOP_DOMAINS = _int("TOP_DOMAINS", 5)                  # how many domains go to Semrush
OPENPAGERANK_API_KEY = os.environ.get("OPENPAGERANK_API_KEY", "")
USE_TRANCO = _bool("USE_TRANCO", True)

# off   = never call Semrush; write semrush_queue.csv and ingest manual exports
# api   = call the Semrush API, but only with SEMRUSH_UNIT_BUDGET set
SEMRUSH_MODE = os.environ.get("SEMRUSH_MODE", "off").lower()
SEMRUSH_API_KEY = os.environ.get("SEMRUSH_API_KEY", "")
SEMRUSH_UNIT_BUDGET = _int("SEMRUSH_UNIT_BUDGET", 0)
SEMRUSH_ROWS = _int("SEMRUSH_ROWS", 30)               # backlink rows per domain
SEMRUSH_DATABASE_HOST = os.environ.get("SEMRUSH_HOST", "https://api.semrush.com")
SEMRUSH_UI_HOST = os.environ.get("SEMRUSH_UI_HOST", "https://www.semrush.com")
MIN_AUTHORITY = _int("MIN_AUTHORITY", 30)             # page AS / authority floor for stage 6

# Optional search APIs. Without any, the job uses seeds.txt plus HTML search scraping.
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
GOOGLE_CSE_KEY = os.environ.get("GOOGLE_CSE_KEY", "")
GOOGLE_CSE_CX = os.environ.get("GOOGLE_CSE_CX", "")


def read_list(name):
    path = CONFIG_DIR / name
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out
