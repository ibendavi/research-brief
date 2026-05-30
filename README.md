# Research Morning Brief

A single-page daily brief of new working papers, journal articles, macro/markets
news, rumors & chatter, and serendipitous "periphery" links — rebuilt every
morning at ~5:00am ET by GitHub Actions and served via GitHub Pages.

## How it works
- **`build.py`** fetches every feed in `sources.json`, dedupes, scores each item
  by *recency + source weight + your topic keywords + your thumbs feedback*,
  and renders `docs/index.html` (+ a dated copy in `docs/archive/`).
- **GitHub Actions** (`.github/workflows/daily.yml`) runs it daily and commits
  the result. Pages serves `docs/`. Runs even when your PC is off.
- **Feedback**: every item has 👍/👎. Clicks are saved in your browser
  (localStorage). Click **Export feedback** to download `feedback.json`.

## Tuning the ranking (manual, v1)
1. Click 👍/👎 on items as you read for a few days.
2. Hit **Export feedback** → it downloads `feedback.json`.
3. Drop that file into this repo's root and commit it. The next build reads it
   and nudges sources you like up / dislike down (`source_net` in `build.py`).
4. For direct control, edit **`weights.json`**:
   - `source_weights`: override a source's priority by name.
   - `boost_keywords`: add points when a term appears (your active themes).
   - `block_keywords`: demote noise.
5. Add/remove feeds in **`sources.json`** (`type` is `rss` or `crossref`;
   Crossref `url` is the journal ISSN). Dead feeds are reported in the build log
   and never break the run.

## Run locally
```
pip install feedparser
python build.py
# open docs/index.html
```

## Schedule
Cron is UTC. `0 9 * * *` ≈ 5am EDT. Switch to `0 10 * * *` for 5am EST in winter.
