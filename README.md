# Morning Brief

A reproducible pipeline that turns public RSS feeds and free market-data APIs into a
daily cross-asset markets brief: a Markdown summary plus two standalone HTML pages
(a data dashboard and a news reader). No API keys required.

**See it in action:** [sample brief](examples/sample-brief.md) ·
[sample dashboard](examples/sample-dashboard.html) ·
[sample news reader](examples/sample-news-reader.html)

## How it fits together

```
briefs/scripts/fetch_news.py         112 public RSS feeds  ->  briefs/data/<date>-news.json
                                                                briefs/data/<date>-news-weekly.json
briefs/scripts/fetch_market_data.py  Yahoo Finance + FRED   ->  briefs/data/<date>-market-data.json
                                                                briefs/data/<date>-heatmap.csv
(hand-authored, from research)                               briefs/data/<date>-narrative.json
briefs/scripts/merge_top_stories.py  folds researched drafts into narrative.json
briefs/scripts/render_news.py        news.json + narrative  ->  briefs/<date>-news.html
briefs/scripts/render_dashboard.py   all of the above       ->  briefs/<date>-dashboard.html
```

Everything quantitative (news pull, market data, HTML rendering) is scripted and
reproducible with no manual editing of HTML. The one step that can't be scripted is
`narrative.json` — synthesizing the day's news into a dated, sourced narrative is a
judgment call, not a data pull.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

D=$(date +%F)
python3 briefs/scripts/fetch_news.py         "$D"   # -> <D>-news.json, <D>-news-weekly.json
python3 briefs/scripts/fetch_market_data.py  "$D"   # -> <D>-market-data.json, <D>-heatmap.csv
# write briefs/data/<D>-narrative.json by hand (copy the shape of the most recent one)
python3 briefs/scripts/merge_top_stories.py  "$D"   # optional: fold a researched draft in
python3 briefs/scripts/render_news.py        "$D"   # -> briefs/<D>-news.html
python3 briefs/scripts/render_dashboard.py   "$D"   # -> briefs/<D>-dashboard.html
```

Run the tests with:

```bash
python3 -m pytest briefs/tests/ -q
```

Generated files land in `briefs/` and `briefs/data/`; both are gitignored since they're
reproducible output, not source (a few samples are kept in `examples/` instead).

## What each piece does

- **`fetch_news.py`** — one concurrent pass over 112 public RSS feeds in three tiers:
  topic-query searches (26 topics × 3 Google News queries), direct publisher/institution
  feeds (CNBC, MarketWatch, the Fed, ECB, BoE, BoJ, Treasury, BLS, EIA, ...), and
  Google News `site:` queries standing in for paywalled outlets (FT, WSJ, Bloomberg,
  Reuters, ...). Filters out noise (corporate-action boilerplate, redundant summaries),
  strips outlet suffixes, and records failed feeds instead of aborting the run.
- **`fetch_market_data.py`** — pulls ~30 sector/factor/asset-class ETF returns from
  Yahoo Finance, 8 macro series from FRED (yields, credit spreads, dollar index, VIX),
  a duration-bucketed Treasury/credit ETF matrix, sector valuation multiples, and full
  yield-curve / credit-curve history sampled at six lookbacks.
- **`lib_news_digest.py`** — turns the raw headline pull into three computed readings
  (attention shift vs. each topic's own trailing median, cross-outlet corroboration,
  and playbook-theme exposure), shared by both HTML renderers so they agree by
  construction.
- **`lib_heatmap.py`** — reusable HTML-fragment renderers for the returns heatmap,
  macro panel, valuation bars, duration matrix, and market-pulse scoreboard.
  Kept separate from `render_dashboard.py` so this logic is unit-testable without
  pulling in Jinja2.
- **`merge_top_stories.py`** — folds a researched top-stories draft
  (`<date>-top-stories-draft.json`) into `narrative.json`, keeping the draft on disk
  as provenance for every dated quote and source.
- **`render_news.py`** — renders `templates/news.html.j2` into a standalone news
  reader: daily/weekly toggle, sticky scroll-spy nav, live filtering, timestamps
  throughout, and a source ledger. Autoescaped, since headlines are untrusted
  third-party text.
- **`render_dashboard.py`** — renders `templates/dashboard.html.j2` into the full
  dashboard: heatmap, macro panel, yield/credit curve overlays and change matrices,
  sector-rotation and credit-curve streamgraphs, global macro matrix, and (when a
  news pull exists for that day) a newsroom section and the computed news analysis.

## Semiconductor technology & market brief

The semiconductor research product is an independent pipeline: it does not modify
the morning brief, general news pull, or their HTML outputs. It has a dedicated
focused RSS search, semiconductor equity tape, dated research file, and standalone
HTML page; the tracked knowledge base provides the persistent industry map,
market-mechanics framework, and glossary.

```bash
D=$(date +%F)
python3 briefs/scripts/build_semiconductor_brief.py "$D" && \
python3 briefs/scripts/synthesize_semiconductor.py "$D" --packet-only && \
(set -o pipefail; cat "briefs/data/$D-semiconductor-prompt.md" \
     "briefs/data/$D-semiconductor-evidence.json" |
  claude -p "Follow the SYSTEM and USER instructions on stdin. Return only the requested raw JSON object." \
    --output-format text > "briefs/data/$D-semiconductor-synthesis.json") && \
python3 briefs/scripts/synthesize_semiconductor.py "$D" \
  --ingest "briefs/data/$D-semiconductor-synthesis.json" && \
python3 briefs/scripts/build_semiconductor_brief.py "$D" --render-only
```

This workflow uses the locally authenticated Claude Code CLI (`claude -p`); it
does not require an `ANTHROPIC_API_KEY` or the Anthropic Python SDK.

The synthesis step hands the model a computed evidence packet (sub-industry
returns, cross-segment correlations and shifts, rates/credit moves, headlines
in week/month/quarter windows, prior theses) and requires cited, falsifiable
themes; ids it cites are validated. Without an API key use `--packet-only` and
`--ingest` (see `semiconductor-brief.md`). The page adds interactive
sub-industry comparison, a correlation explorer, and a rates/credit lab.

Outputs are `briefs/data/<D>-semiconductor-data.json`,
`<D>-semiconductor-evidence.json`, `briefs/data/<D>-semiconductor-research.json`, and
`briefs/<D>-semiconductor.html`. The page uses its own template
(`briefs/templates/semiconductor.html.j2`) and tracked reference
(`briefs/semiconductors/knowledge.json`). It presents adjusted returns and
price-times-volume activity as descriptive proxies only—not ETF flows,
order-book depth, or positioning. See [`semiconductor-brief.md`](semiconductor-brief.md)
for the research checklist, source rules, and the dated research-file schema.

## Repository layout

```
brief.md                  Task specification: the research checklist and editorial
                           rules a research pass follows when writing narrative.json.
playbook.md               The macro/cross-asset investment framework used to tag
                           stories to structural themes (referenced by brief.md).
semiconductor-brief.md    Separate semiconductor research workflow and evidence rules.
briefs/
  scripts/                All pipeline code (see above).
  templates/              Jinja2 templates for the dashboards, news reader, and
                           semiconductor research page.
  semiconductors/          Tracked semiconductor industry and market-mechanics knowledge base.
  tests/                   Unit tests (no network) for the fetchers and render helpers.
  data/                    Generated JSON/CSV data pulls (gitignored).
  *.md, *.html             Generated daily briefs and dashboards (gitignored).
examples/                  Checked-in samples of the generated output.
```

## Editorial rules the narrative follows

Anything hand-authored into `narrative.json` follows the rules in
[`brief.md`](brief.md): every number needs a source and timestamp, disagreeing sources
are shown side by side rather than averaged, "analysts say" is never used without
naming the person and institution, and reported fact / attributed analysis / editorial
"Read:" are kept visibly distinct.
