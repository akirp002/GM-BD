---
description: Semiconductor technology, industry structure, and market microstructure brief
---

# Semiconductor Technology & Market Brief

Use this as a separate daily research product from `brief.md`. The dedicated
builder is `briefs/scripts/build_semiconductor_brief.py`; it writes
`briefs/data/YYYY-MM-DD-semiconductor-data.json` and
`briefs/YYYY-MM-DD-semiconductor.html`. It does not modify the morning brief,
dashboard, or general news-reader outputs.

## Purpose

Build a useful, cumulative learning and research tool for semiconductor
technology, the global industry, and the market structure around semiconductor
equities. The daily page combines:

1. A sourced, timestamped market snapshot and focused semiconductor headlines.
2. Human research notes that distinguish reported facts, attributed analysis,
   and the editor's "Read:".
3. A durable industry map and glossary in
   `briefs/semiconductors/knowledge.json`.

The dated HTML is a reading surface; keep structured raw pulls and edits to
the durable knowledge file so the work remains searchable and reproducible.

## Refresh workflow

```bash
D=$(date +%F)
python3 briefs/scripts/build_semiconductor_brief.py "$D" && \
python3 briefs/scripts/synthesize_semiconductor.py "$D" && \
python3 briefs/scripts/build_semiconductor_brief.py "$D" --render-only
```

The LLM step requires `ANTHROPIC_API_KEY` and the `anthropic` package
(`pip install anthropic`).

No API key? Use a Claude Code session as the analyst:

```bash
python3 briefs/scripts/synthesize_semiconductor.py "$D" --packet-only   # writes <D>-semiconductor-evidence.json + prompt
# Claude reads the packet, writes a synthesis JSON following the schema in
# the SYSTEM prompt, then:
python3 briefs/scripts/synthesize_semiconductor.py "$D" --ingest my-synthesis.json
```

What step 1 pulls: three news windows (week, 8-30 days, 31-95 days) with
stable citation ids and a durable `briefs/semiconductors/headline_archive.jsonl`
for older context; 2 years of prices for ~35 stocks in 9 sub-industry segments
plus SOXX/SMH/XSD/SPY/QQQ; and 16 FRED series (Treasury and real yields,
breakevens, IG/BBB/HY/BB/B/CCC OAS, VIX, dollar, bank delinquency/charge-off).

## How the synthesis stays grounded

`synthesize_semiconductor.py` builds an **evidence packet** of *computed*
facts, not just links: multi-horizon returns and relative strength by segment,
cross-segment correlations and how they shifted vs the prior window,
dispersion and drawdowns, rates/credit moves with percentiles, the
semis-vs-credit correlations, headlines by window, and last period's theses
(`briefs/semiconductors/thesis_log.jsonl`) so views evolve instead of
resetting. The model must produce week/month/quarter syntheses, ranked themes
with evidence, counter-evidence, falsifiers and a change-vs-prior note,
industry linkages, a rates/credit read, and open questions. Citations may only
use ids in the packet; `validate_synthesis` removes unknown ids and warns on
themes with no falsifier, no counter-evidence or fewer than two evidence items.
Warnings appear on the page. Synthesis is analysis, not fact: verify claims
that rest on a single secondary outlet.

Default model is `claude-opus-5-5` (override with `--model` or `SEMI_BRIEF_MODEL`).

## Default rates

Rated-bond default rates are licensed data. The page shows what is free: a
spread-implied loss gauge (OAS / (1 - 40% recovery), a stress gauge, not a
default rate) and the Fed's quarterly bank delinquency/charge-off rates. To add
Moody's/S&P/Fitch figures, create `briefs/semiconductors/default_rates.json`:

```json
{"series": [{"label": "US spec-grade default rate (Moody's, TTM)", "value": "3.1%", "source": "Moody's", "as_of": "2026-09-30"}]}
```

## Dashboard

The page is interactive and remembers choices per browser: rank sub-industries
by any horizon, click rows/chips to compare segments, ETFs, benchmarks and
single stocks (or build an equal-weight basket), pick the range; a correlation
explorer (sub-industries, +ETFs, all stocks, or your selection; 20/60/120d;
level or change vs the prior window); a rates/credit lab (change in OAS by
rating, chart any series with level/change views, SOXX panel for comparison);
and a Customize drawer to hide sections.

## Research checklist

1. **Technology and product:** What changed in architecture, process node,
   lithography, memory, packaging, power, or software? Explain what is technically
   new versus a roadmap claim, and identify the production/qualification stage.
2. **Value chain:** Which company or stage captures the economics: IP/design,
   fabless chip, foundry, IDM, equipment/materials, memory, advanced packaging/
   OSAT, systems, or distribution? Identify dependencies and customer/supplier
   concentration.
3. **Demand and supply:** Separate end demand from orders, channel inventory,
   wafer starts, utilization, yield, capacity announcements, qualified output,
   and shipment. State product, geography, period, and source definitions.
4. **Financial drivers:** Track revenue mix, gross margin, inventory and days,
   capex, depreciation, backlog, cash conversion, and customer concentration.
   Compare the company's own disclosure with independent industry series where
   possible.
5. **Market structure:** Contrast cap-weighted and equal-weight sector returns;
   distinguish price performance from ETF flows; consider index concentration,
   breadth, earnings revisions, liquidity, options expiry/positioning, and
   rates. Do not infer investor intent from price/volume alone.
6. **Catalysts and falsifiers:** List dated earnings, product ramps, capacity
   milestones, policy deadlines, and data releases. State what evidence would
   confirm or weaken the thesis.
7. **Learning:** Add stable concepts and mechanisms to
   `briefs/semiconductors/knowledge.json`. Keep transient facts, forecasts, and
   dated company claims in the daily research file.

## Evidence and market-data rules

- Every quantitative claim needs a source, unit, period, and as-of/publication
  date. Preserve differing definitions; do not average incompatible measures.
- Prefer company filings and IR, government notices, WSTS/SIA, and SEMI for
  primary claims. News feeds are discovery, not independent fact checking.
- Attribute forecasts and opinions to a named person or organization. Do not
  turn a single-company comment into sector consensus.
- Keep **Reported**, **Analysis**, and **Read:** visibly distinct. Label
  uncertainty and name the evidence that could falsify the read.
- Distinguish announced capex from installed capacity, wafer starts from
  completed good dies, backlog from revenue, bookings from billings, and
  channel shipments from sell-through.
- Price × reported share volume is only a trading-activity proxy. It is not
  order-book depth, market impact, ETF flow, dealer positioning, or proof of
  causation. Options open interest alone does not reveal who is long or short.
- A null or stale observation is not zero. Preserve the source's as-of date and
  display fetch failures; do not fill missing values with current headlines.

## Legacy research-file fields

`top_stories`, `watchlist` and `learning_notes` (below) are still rendered. The
synthesis adds `bottom_line`, `horizons`, `themes`, `industry_linkages`,
`rates_credit` and `open_questions`. The builder creates
`briefs/data/YYYY-MM-DD-semiconductor-research.json`:

```json
{
  "as_of": "YYYY-MM-DD",
  "top_stories": [
    {
      "title": "Specific development",
      "fact": "What a primary source reported, including dates and units.",
      "analysis": "A named source's explanation or forecast, attributed.",
      "read": "Why it matters; distinguish inference from observation.",
      "sources": [
        {"label": "Company filing or primary source", "url": "https://...", "published": "YYYY-MM-DD"}
      ]
    }
  ],
  "watchlist": [
    {"signal": "Dated catalyst or measurable indicator", "why": "What it could confirm or falsify.", "url": "https://..."}
  ],
  "learning_notes": []
}
```

Keep the top stories selective. Prefer a verified technology or supply-chain
mechanism over a recap of stock headlines. Use `briefs/semiconductors/knowledge.json`
for durable explanations that should remain useful after this week's news fades.
