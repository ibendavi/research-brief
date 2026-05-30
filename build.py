#!/usr/bin/env python3
"""Research Morning Brief — daily static-site builder.

Fetches a curated set of RSS/Atom feeds and Crossref journal queries, scores
items by recency + manual source/keyword weights + accumulated thumbs feedback,
and renders a single-file HTML brief into docs/ (served by GitHub Pages).

Robust by design: every source is fetched in a try/except; dead feeds are
reported in the build log and never abort the run.
"""

import json
import re
import sys
import html
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ARCHIVE = DOCS / "archive"

# Section order, display titles, and how many items to show in each.
SECTIONS = [
    ("papers",    "New Working Papers",          14),
    ("journals",  "Just-Published Journal Articles", 10),
    ("macro",     "Macro & Markets",             10),
    ("chatter",   "Rumors & Chatter",             8),
    ("periphery", "From the Periphery",           6),
]

USER_AGENT = "ResearchBriefBot/1.0 (+https://github.com/ibendavi)"
# How far back each section looks (days).
SECTION_WINDOW_DAYS = {
    "papers": 10, "journals": 21, "macro": 3, "chatter": 4, "periphery": 7,
}
NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def fetch_rss(src) -> list[dict]:
    feed = feedparser.parse(src["url"], agent=USER_AGENT)
    items = []
    for e in feed.entries:
        link = e.get("link") or ""
        title = _strip_html(e.get("title") or "")
        if not title or not link:
            continue
        summary = _strip_html(e.get("summary") or e.get("description") or "")
        items.append({
            "title": title,
            "link": link,
            "summary": summary[:400],
            "source": src["name"],
            "section": src["section"],
            "weight": src.get("weight", 1.0),
            "published": _parse_date(e),
        })
    return items


def fetch_crossref(src) -> list[dict]:
    issn = src["url"]
    since = (NOW - timedelta(days=SECTION_WINDOW_DAYS.get(src["section"], 30))).date()
    api = (f"https://api.crossref.org/journals/{issn}/works"
           f"?filter=from-pub-date:{since},type:journal-article"
           f"&sort=published&order=desc&rows=20"
           f"&select=title,DOI,published,abstract,author")
    req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    items = []
    for w in data.get("message", {}).get("items", []):
        title_list = w.get("title") or []
        if not title_list:
            continue
        title = _strip_html(title_list[0])
        doi = w.get("DOI", "")
        link = f"https://doi.org/{doi}" if doi else ""
        parts = (w.get("published", {}).get("date-parts") or [[None]])[0]
        pub = None
        if parts and parts[0]:
            y = parts[0]
            m = parts[1] if len(parts) > 1 else 1
            d = parts[2] if len(parts) > 2 else 1
            try:
                pub = datetime(y, m, d, tzinfo=timezone.utc)
            except Exception:
                pub = None
        authors = ", ".join(
            f"{a.get('family','')}".strip() for a in (w.get("author") or [])[:4]
        )
        summary = _strip_html(w.get("abstract") or "")
        if authors:
            summary = f"{authors}. {summary}" if summary else authors
        items.append({
            "title": title, "link": link, "summary": summary[:400],
            "source": src["name"], "section": src["section"],
            "weight": src.get("weight", 1.0), "published": pub,
        })
    return items


def fetch_all(sources) -> tuple[list[dict], list[str]]:
    items, log = [], []
    for src in sources:
        try:
            got = fetch_crossref(src) if src["type"] == "crossref" else fetch_rss(src)
            log.append(f"  OK   {len(got):3d}  {src['name']}")
            items.extend(got)
        except Exception as exc:  # noqa: BLE001 — never let one feed kill the run
            log.append(f"  FAIL   0  {src['name']}  ({type(exc).__name__}: {exc})")
    return items, log


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def item_id(item) -> str:
    return hashlib.sha1(item["link"].encode("utf-8")).hexdigest()[:12]


def score_item(item, weights, feedback) -> float:
    pub = item["published"]
    section = item["section"]
    window = SECTION_WINDOW_DAYS.get(section, 14)
    if pub is None:
        age_days = window  # unknown date -> treat as old
    else:
        age_days = max(0.0, (NOW - pub).total_seconds() / 86400.0)
    # Recency: linear decay across the section's window, floored at 0.
    recency = max(0.0, 1.0 - age_days / max(window, 1)) * 30.0

    base = 10.0 * weights["source_weights"].get(item["source"], item["weight"])

    hay = (item["title"] + " " + item["summary"]).lower()
    boost = sum(pts for kw, pts in weights["boost_keywords"].items()
                if kw.lower() in hay)
    block = sum(pts for kw, pts in weights["block_keywords"].items()
                if kw.lower() in hay)

    # Feedback: net thumbs accumulated per source (read back from feedback.json).
    fb = feedback.get("source_net", {}).get(item["source"], 0) * 2.0

    return recency + base + boost - block + fb


def load_feedback() -> dict:
    """feedback.json is the exported thumbs file (optional). We pre-aggregate
    net thumbs per source so scoring stays O(1)."""
    path = ROOT / "feedback.json"
    agg = {"source_net": {}, "items": {}}
    if not path.exists():
        return agg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return agg
    for entry in raw.get("votes", []):
        src = entry.get("source", "")
        v = 1 if entry.get("vote") == "up" else -1 if entry.get("vote") == "down" else 0
        agg["source_net"][src] = agg["source_net"].get(src, 0) + v
        agg["items"][entry.get("id", "")] = entry.get("vote")
    return agg


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(sections_items, build_time, feedback) -> str:
    date_str = build_time.strftime("%A, %B %-d, %Y") if sys.platform != "win32" \
        else build_time.strftime("%A, %B %d, %Y")
    blocks = []
    for key, title, _cap in SECTIONS:
        items = sections_items.get(key, [])
        if not items:
            continue
        cards = []
        for it in items:
            iid = it["_id"]
            pub = it["published"]
            age = ""
            if pub:
                hrs = (NOW - pub).total_seconds() / 3600.0
                age = f"{int(hrs)}h ago" if hrs < 48 else f"{int(hrs/24)}d ago"
            prior = feedback.get("items", {}).get(iid, "")
            up_cls = " voted" if prior == "up" else ""
            dn_cls = " voted" if prior == "down" else ""
            summary = html.escape(it["summary"])
            cards.append(f"""
      <article class="card" data-id="{iid}" data-source="{html.escape(it['source'])}">
        <h3><a href="{html.escape(it['link'])}" target="_blank" rel="noopener">{html.escape(it['title'])}</a></h3>
        <p class="summary">{summary}</p>
        <div class="meta">
          <span class="src">{html.escape(it['source'])}</span>
          <span class="age">{age}</span>
          <span class="vote">
            <button class="up{up_cls}" onclick="vote('{iid}','{html.escape(it['source'])}','up',this)">&#128077;</button>
            <button class="down{dn_cls}" onclick="vote('{iid}','{html.escape(it['source'])}','down',this)">&#128078;</button>
          </span>
        </div>
      </article>""")
        blocks.append(
            f'    <section>\n      <h2>{html.escape(title)}</h2>\n'
            + "\n".join(cards) + "\n    </section>"
        )
    body = "\n".join(blocks)
    return PAGE_TEMPLATE.format(date=date_str, body=body,
                               built=build_time.strftime("%Y-%m-%d %H:%M UTC"))


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Research Morning Brief — {date}</title>
<style>
  :root {{
    --bg:#faf8f3; --card:#fff; --ink:#1a1a1a; --muted:#6b6b6b;
    --line:#e6e1d6; --accent:#7a1f1f; --link:#1a4f8a;
    --fs:1.5; /* 150% scaling per preference */
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#16171a; --card:#1f2125; --ink:#e8e6e1; --muted:#9a978f;
             --line:#33363b; --accent:#e0a0a0; --link:#7fb0e8; }}
  }}
  html {{ font-size: calc(16px * var(--fs)); }}
  body {{ margin:0; background:var(--bg); color:var(--ink);
          font-family: Georgia, 'Times New Roman', serif; line-height:1.5; }}
  header {{ position:sticky; top:0; background:var(--bg); border-bottom:2px solid var(--accent);
            padding:0.6rem 1rem; z-index:10; display:flex; justify-content:space-between;
            align-items:baseline; flex-wrap:wrap; gap:0.5rem; }}
  header h1 {{ font-size:1.25rem; margin:0; color:var(--accent); letter-spacing:0.5px; }}
  header .date {{ color:var(--muted); font-size:0.8rem; }}
  header button {{ font-size:0.7rem; cursor:pointer; background:var(--card);
                   border:1px solid var(--line); color:var(--ink); padding:0.3rem 0.6rem;
                   border-radius:6px; font-family:inherit; }}
  main {{ max-width:54rem; margin:0 auto; padding:1rem; }}
  section {{ margin-bottom:2rem; }}
  section h2 {{ font-size:1.05rem; color:var(--accent); border-bottom:1px solid var(--line);
               padding-bottom:0.3rem; margin:1.5rem 0 0.8rem; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:8px;
           padding:0.8rem 1rem; margin-bottom:0.7rem; }}
  .card h3 {{ font-size:0.95rem; margin:0 0 0.3rem; line-height:1.35; }}
  .card h3 a {{ color:var(--link); text-decoration:none; }}
  .card h3 a:hover {{ text-decoration:underline; }}
  .summary {{ font-size:0.8rem; color:var(--ink); margin:0.2rem 0 0.5rem;
              font-family: -apple-system, system-ui, sans-serif; opacity:0.85; }}
  .meta {{ display:flex; align-items:center; gap:0.8rem; font-size:0.7rem;
           color:var(--muted); font-family:system-ui, sans-serif; }}
  .meta .src {{ font-weight:600; }}
  .meta .vote {{ margin-left:auto; }}
  .vote button {{ background:none; border:none; cursor:pointer; font-size:0.95rem;
                  opacity:0.45; padding:0 0.15rem; }}
  .vote button:hover {{ opacity:0.9; }}
  .vote button.voted {{ opacity:1; transform:scale(1.15); }}
  footer {{ text-align:center; color:var(--muted); font-size:0.7rem; padding:2rem 1rem; }}
  footer a {{ color:var(--link); }}
</style>
</head>
<body>
<header>
  <div><h1>Research Morning Brief</h1><div class="date">{date}</div></div>
  <div>
    <button onclick="exportFeedback()">Export feedback</button>
    <a href="archive/index.html"><button>Archive</button></a>
  </div>
</header>
<main>
{body}
</main>
<footer>
  Built {built} &middot; rebuilds daily ~5:00am ET &middot;
  thumbs are saved in your browser; click <em>Export feedback</em> to tune ranking.
</footer>
<script>
const KEY = 'rmb_feedback';
function load() {{ try {{ return JSON.parse(localStorage.getItem(KEY)) || {{}}; }} catch(e) {{ return {{}}; }} }}
function save(o) {{ localStorage.setItem(KEY, JSON.stringify(o)); }}
function vote(id, source, dir, btn) {{
  const o = load();
  if (o[id] && o[id].vote === dir) {{ delete o[id]; }}
  else {{ o[id] = {{ id, source, vote: dir, ts: new Date().toISOString() }}; }}
  save(o);
  const card = btn.closest('.card');
  card.querySelectorAll('.vote button').forEach(b => b.classList.remove('voted'));
  if (o[id]) btn.classList.add('voted');
}}
function exportFeedback() {{
  const o = load();
  const votes = Object.values(o);
  const blob = new Blob([JSON.stringify({{votes}}, null, 2)], {{type:'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'feedback.json';
  a.click();
}}
// Re-apply any votes stored locally that the server build didn't yet know about.
window.addEventListener('DOMContentLoaded', () => {{
  const o = load();
  document.querySelectorAll('.card').forEach(card => {{
    const id = card.dataset.id;
    if (o[id]) {{
      const sel = o[id].vote === 'up' ? '.up' : '.down';
      const b = card.querySelector('.vote ' + sel);
      if (b) b.classList.add('voted');
    }}
  }});
}});
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #
def write_archive_index():
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    files = sorted(ARCHIVE.glob("brief_*.html"), reverse=True)
    links = "\n".join(
        f'    <li><a href="{f.name}">{f.stem.replace("brief_", "")}</a></li>'
        for f in files
    )
    (ARCHIVE / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brief Archive</title>
<style>body{{font-family:Georgia,serif;max-width:40rem;margin:2rem auto;padding:1rem;
font-size:1.4rem;line-height:1.8;background:#faf8f3;color:#1a1a1a}}
a{{color:#1a4f8a}}h1{{color:#7a1f1f}}</style></head>
<body><h1>Brief Archive</h1>
<p><a href="../index.html">&larr; Today</a></p>
<ul>
{links}
</ul></body></html>""", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    cfg = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    weights = json.loads((ROOT / "weights.json").read_text(encoding="utf-8"))
    weights.setdefault("source_weights", {})
    weights.setdefault("boost_keywords", {})
    weights.setdefault("block_keywords", {})
    feedback = load_feedback()

    print("Fetching sources…")
    items, log = fetch_all(cfg["sources"])
    print("\n".join(log))
    print(f"\nTotal raw items: {len(items)}")

    # Dedupe by link.
    seen, deduped = set(), []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        it["_id"] = item_id(it)
        deduped.append(it)

    # Window filter + score + group + cap.
    sections_items = {}
    for key, _title, cap in SECTIONS:
        window = SECTION_WINDOW_DAYS.get(key, 14)
        pool = []
        for it in deduped:
            if it["section"] != key:
                continue
            pub = it["published"]
            if pub is not None and (NOW - pub).days > window:
                continue
            it["_score"] = score_item(it, weights, feedback)
            pool.append(it)
        pool.sort(key=lambda x: x["_score"], reverse=True)
        sections_items[key] = pool[:cap]
        print(f"  {key:10s}: {len(pool):3d} in window -> showing {len(sections_items[key])}")

    html_out = render(sections_items, NOW, feedback)
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.html").write_text(html_out, encoding="utf-8")
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    (ARCHIVE / f"brief_{NOW.strftime('%Y-%m-%d')}.html").write_text(html_out, encoding="utf-8")
    write_archive_index()
    print(f"\nWrote {DOCS/'index.html'}")


if __name__ == "__main__":
    main()
