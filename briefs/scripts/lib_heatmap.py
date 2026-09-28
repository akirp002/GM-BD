"""
Reusable renderers that turn the quantitative pull (fetch_market_data.py's
output) into HTML fragments. Kept separate from render_dashboard.py so the
heatmap/macro-panel logic can be unit-tested or reused without dragging in
Jinja2 or the narrative content.
"""
import json
import math
from pathlib import Path
from typing import Optional

import pandas as pd

PERIOD_COLS = ["1D", "1W", "1M", "3M", "YTD"]
CATEGORY_LABELS = {"sector": "Sectors", "factor": "Factors", "asset": "Asset classes"}


def _cell_html(value: Optional[float], cap: float = 15.0, invert: bool = False, suffix: str = "", title: str = "") -> str:
    """Color-coded table cell. `invert=True` flips the up/down color mapping
    -- used for yield/spread *changes*, where a rise (positive) is bad for
    bond prices and should read red, not the "positive = green" convention
    used for equity-style returns."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return '<td class="hm-cell">—</td>'
    intensity = min(abs(value) / cap, 1.0)
    pct = round(8 + intensity * 72, 1)
    is_up = value >= 0
    token = ("down" if is_up else "up") if invert else ("up" if is_up else "down")
    sign = "+" if value > 0 else ""
    title_attr = f' title="{title}"' if title else ""
    return (
        f'<td class="hm-cell"{title_attr} style="background:color-mix(in srgb, var(--{token}) '
        f'{pct}%, transparent);">{sign}{value:.1f}{suffix}</td>'
    )


def build_heatmap_table_html(heatmap_csv: Path, cap: float = 15.0) -> str:
    """Render the full <tbody> rows for the returns heatmap from the CSV
    written by fetch_market_data.py: ticker, category, label, 1D, 1W, 1M, 3M, YTD."""
    df = pd.read_csv(heatmap_csv)
    rows = []
    for category, label in CATEGORY_LABELS.items():
        subset = df[df["category"] == category].sort_values("ticker")
        if subset.empty:
            continue
        rows.append(f'<tr class="hm-group"><td colspan="7">{label}</td></tr>')
        for _, r in subset.iterrows():
            cells = "".join(_cell_html(r[p], cap) for p in PERIOD_COLS)
            rows.append(
                f'<tr><td class="hm-ticker">{r.ticker}</td>'
                f'<td class="hm-label">{r.label}</td>{cells}</tr>'
            )
    return "\n".join(rows)


def build_macro_panel_html(market_data_json: Path) -> str:
    """Render the macro-panel tiles from the FRED pull in market-data.json."""
    payload = json.loads(Path(market_data_json).read_text())
    macro = payload["macro"]
    tiles = []
    for sid, info in macro.items():
        if info["value"] is None:
            continue
        chg = info["chg_1m"]
        # Rates/spreads/dollar: a rise is "risk-off" red; a fall is "risk-on" green.
        # This is a display heuristic, not a judgment about direction being good/bad.
        color = "var(--down)" if (chg or 0) >= 0 else "var(--up)"
        chg_str = f"{'+' if (chg or 0) > 0 else ''}{chg:.2f} (1m)" if chg is not None else "—"
        tiles.append(
            '<div class="macro-tile">'
            f'<div class="k">{info["label"]}</div>'
            f'<div class="v">{info["value"]}</div>'
            f'<div class="d" style="color:{color};">{chg_str}</div>'
            f'<div class="asof">FRED {sid} · {info["as_of"]}</div>'
            "</div>"
        )
    return "\n".join(tiles)


def build_valuation_bars_html(market_data_json: Path) -> str:
    """Diverging bar rows: each sector's trailing P/E relative to the
    sector-peer average (z-score), plus its premium/discount to SPY."""
    payload = json.loads(Path(market_data_json).read_text())
    valuation = payload.get("valuation", {})
    rows = []
    for tkr, info in valuation.items():
        if tkr == "_peer_group" or info.get("peer_zscore") is None:
            continue
        z = info["peer_zscore"]
        cap = 2.0
        pct_width = round(min(abs(z) / cap, 1.0) * 50, 1)
        token = "down" if z > 0 else "up"  # richer (higher PE) reads as "hot" / red
        side = "right" if z > 0 else "left"
        pe = info.get("trailing_pe")
        prem = info.get("premium_to_spy_pct")
        prem_str = f"{'+' if (prem or 0) > 0 else ''}{prem}%" if prem is not None else "—"
        rows.append(
            '<div class="val-row">'
            f'<div class="val-label">{tkr} <span>{info["label"]}</span></div>'
            '<div class="val-bar-track">'
            f'<div class="val-bar val-bar-{side}" style="width:{pct_width}%;'
            f'background:color-mix(in srgb, var(--{token}) 65%, transparent);"></div>'
            '<div class="val-zero"></div>'
            "</div>"
            f'<div class="val-pe">{pe if pe is not None else "—"}<small>PE</small></div>'
            f'<div class="val-prem">{prem_str}<small>vs SPY</small></div>'
            "</div>"
        )
    return "\n".join(rows)


def build_duration_matrix_html(market_data_json: Path) -> str:
    """Table rows for the fixed-income duration matrix: ticker, duration,
    level, and returns across lookback windows."""
    payload = json.loads(Path(market_data_json).read_text())
    fi = payload.get("fi_matrix", {})
    rows = []
    for tkr, info in sorted(fi.items(), key=lambda kv: kv[1].get("duration_years") or 0):
        dur = info.get("duration_years")
        cells = "".join(_cell_html(info["returns"].get(p)) for p in PERIOD_COLS)
        rows.append(
            f'<tr><td class="hm-ticker">{tkr}</td><td class="hm-label">{info["label"]}</td>'
            f'<td class="lvl" style="text-align:center;">{dur if dur is not None else "—"}y</td>'
            f"{cells}</tr>"
        )
    return "\n".join(rows)


def build_credit_curve_html(market_data_json: Path) -> str:
    """Bar-chart rows for today's OAS by rating bucket, AAA (tightest) to CCC
    (widest) -- the credit curve's current shape, laid out as horizontal bars.
    (The multi-snapshot evolution overlay is a separate SVG, built in
    render_dashboard.py from the same payload's `credit_curve.snapshots`.)"""
    payload = json.loads(Path(market_data_json).read_text())
    curve = payload.get("credit_curve", {})
    order = curve.get("ratings", [])
    now = curve.get("snapshots", {}).get("Now", {})
    values = [now[r]["value_bp"] for r in order if now.get(r, {}).get("value_bp") is not None]
    max_v = max(values) if values else 1
    rows = []
    for rating in order:
        info = now.get(rating, {})
        v = info.get("value_bp")
        if v is None:
            rows.append(f'<div class="cc-row"><div class="cc-label">{rating}</div><div class="cc-bar-track">—</div></div>')
            continue
        pct_width = round(v / max_v * 100, 1)
        token = "down" if rating in ("BB", "B", "CCC") else "up"
        rows.append(
            '<div class="cc-row">'
            f'<div class="cc-label">{rating}</div>'
            '<div class="cc-bar-track">'
            f'<div class="cc-bar" style="width:{pct_width}%;background:color-mix(in srgb, var(--{token}) 55%, transparent);">'
            f'<span>{v}bp</span></div>'
            "</div>"
            f'<div class="cc-asof">{info.get("as_of") or "—"}</div>'
            "</div>"
        )
    return "\n".join(rows)


# Snapshot labels, short-to-long, excluding "Now" -- the column order for
# every change matrix below. Matches fetch_market_data.py's
# CURVE_SNAPSHOT_OFFSETS keys (which are stored oldest-to-newest).
DELTA_LABELS = ["1W ago", "1M ago", "3M ago", "1Y ago", "3Y ago"]


def build_yield_change_matrix_html(market_data_json: Path, cap_bp: float = 75.0) -> str:
    """Color-coded table: each Treasury tenor's level today plus its change
    (in bp) versus 1W/1M/3M/1Y/3Y ago -- the "how much has this moved"
    answer that's hard to read off an overlaid line chart by eye."""
    payload = json.loads(Path(market_data_json).read_text())
    yc = payload.get("yield_curve", {})
    tenors, snapshots = yc.get("tenors", []), yc.get("snapshots", {})
    now = snapshots.get("Now", {})
    rows = []
    for t in tenors:
        now_v = now.get(t)
        now_cell = f'<td class="lvl">{now_v:.2f}%</td>' if now_v is not None else '<td class="lvl">—</td>'
        cells = [now_cell]
        for label in DELTA_LABELS:
            past_v = snapshots.get(label, {}).get(t)
            delta = round((now_v - past_v) * 100, 1) if (now_v is not None and past_v is not None) else None
            title = f"{t}: {past_v}% ({label}) -> {now_v}% (now)" if delta is not None else ""
            cells.append(_cell_html(delta, cap=cap_bp, invert=True, suffix="bp", title=title))
        rows.append(f'<tr><td class="hm-ticker">{t}</td>{"".join(cells)}</tr>')
    return "\n".join(rows)


def build_credit_change_matrix_html(market_data_json: Path, cap_bp: float = 150.0) -> str:
    """Same change matrix as yields, for OAS by rating bucket (values already
    in bp, so no unit conversion needed)."""
    payload = json.loads(Path(market_data_json).read_text())
    cc = payload.get("credit_curve", {})
    ratings, snapshots = cc.get("ratings", []), cc.get("snapshots", {})
    now = snapshots.get("Now", {})
    rows = []
    for r in ratings:
        now_v = now.get(r, {}).get("value_bp")
        now_cell = f'<td class="lvl">{now_v:.0f}bp</td>' if now_v is not None else '<td class="lvl">—</td>'
        cells = [now_cell]
        for label in DELTA_LABELS:
            past_v = snapshots.get(label, {}).get(r, {}).get("value_bp")
            delta = round(now_v - past_v, 1) if (now_v is not None and past_v is not None) else None
            title = f"{r}: {past_v}bp ({label}) -> {now_v}bp (now)" if delta is not None else ""
            cells.append(_cell_html(delta, cap=cap_bp, invert=True, suffix="bp", title=title))
        rows.append(f'<tr><td class="hm-ticker">{r}</td>{"".join(cells)}</tr>')
    return "\n".join(rows)


# Column order for the cross-country macro matrices below -- matches
# fetch_market_data.py's GLOBAL_MACRO_HORIZONS keys (stored shortest-first).
GLOBAL_MACRO_HORIZON_LABELS = ["1M", "2M", "3M", "6M", "12M", "2Y", "3Y"]


def _global_macro_row(country: str, info: dict, cap: float, suffix: str, invert: bool, mode: str) -> str:
    """One row of a cross-country macro matrix. `mode='delta'` colors each
    cell by (now - horizon-ago value) -- a change matrix, for rate/inflation.
    `mode='level'` colors each cell by the raw value that prevailed at that
    horizon -- a growth-trajectory matrix, for output/consumption where the
    series is already a QoQ growth rate and the interesting signal is the
    print itself at each point back in time, not a delta-of-a-delta."""
    now_v = info.get("now")
    now_cell = f'<td class="lvl">{now_v:.1f}{suffix}</td>' if now_v is not None else '<td class="lvl">—</td>'
    cells = [now_cell]
    horizons = info.get("horizons", {})
    for label in GLOBAL_MACRO_HORIZON_LABELS:
        past_v = horizons.get(label)
        if now_v is None or past_v is None:
            cells.append(_cell_html(None))
            continue
        if mode == "delta":
            value = round(now_v - past_v, 2)
            title = f"{country}: {past_v}{suffix} ({label} ago) -> {now_v}{suffix} (now)"
        else:
            value = past_v
            title = f"{country}: {past_v}{suffix} as of {label} ago"
        cells.append(_cell_html(value, cap=cap, invert=invert, suffix=suffix, title=title))
    return f'<tr><td class="hm-ticker">{country}</td>{"".join(cells)}</tr>'


def build_global_rate_change_matrix_html(market_data_json: Path, cap_bp: float = 300.0) -> str:
    """Cross-country policy-rate change matrix: each central bank's rate now
    plus its change (in bp) over 1M..3Y -- how far and how fast each bank has
    moved, side by side, not just the Fed."""
    payload = json.loads(Path(market_data_json).read_text())
    gm = payload.get("global_macro", {})
    rows = []
    for country, indicators in gm.items():
        rate = indicators.get("rate", {})
        # bp, not pp: multiply the pp delta by 100 inside the row builder's
        # cap/values by pre-scaling now/horizons here.
        scaled = {
            "now": rate["now"] * 100 if rate.get("now") is not None else None,
            "horizons": {k: (v * 100 if v is not None else None) for k, v in rate.get("horizons", {}).items()},
        }
        rows.append(_global_macro_row(country, scaled, cap=cap_bp, suffix="bp", invert=True, mode="delta"))
    return "\n".join(rows)


def build_global_inflation_change_matrix_html(market_data_json: Path, cap_pp: float = 8.0) -> str:
    """Cross-country CPI YoY change matrix: each country's inflation rate now
    plus its change (in pp) over 1M..3Y -- who's disinflating vs. reflating,
    and how much of the last cycle's move has unwound."""
    payload = json.loads(Path(market_data_json).read_text())
    gm = payload.get("global_macro", {})
    rows = []
    for country, indicators in gm.items():
        cpi = indicators.get("cpi_yoy", {})
        rows.append(_global_macro_row(country, cpi, cap=cap_pp, suffix="pp", invert=True, mode="delta"))
    return "\n".join(rows)


def build_global_output_growth_matrix_html(market_data_json: Path, cap_pct: float = 2.5) -> str:
    """Cross-country output-growth trajectory: each country's GDP QoQ growth
    rate as it stood 1M..3Y ago -- a heatmap of the growth trend itself
    (accelerating/decelerating/contracting), not a change-of-a-change."""
    payload = json.loads(Path(market_data_json).read_text())
    gm = payload.get("global_macro", {})
    rows = []
    for country, indicators in gm.items():
        output = indicators.get("output_qoq", {})
        rows.append(_global_macro_row(country, output, cap=cap_pct, suffix="%", invert=False, mode="level"))
    return "\n".join(rows)


def build_global_consumption_growth_matrix_html(market_data_json: Path, cap_pct: float = 2.5) -> str:
    """Cross-country household-consumption growth trajectory, same shape as
    the output-growth matrix above."""
    payload = json.loads(Path(market_data_json).read_text())
    gm = payload.get("global_macro", {})
    rows = []
    for country, indicators in gm.items():
        cons = indicators.get("consumption_qoq", {})
        rows.append(_global_macro_row(country, cons, cap=cap_pct, suffix="%", invert=False, mode="level"))
    return "\n".join(rows)


def build_global_output_change_matrix_html(market_data_json: Path, cap_pp: float = 2.0) -> str:
    """Cross-country output-growth *change* matrix -- same delta framing as
    the rate and CPI matrices above (now vs. horizon-ago), but on the growth
    rate itself: a positive cell means growth has accelerated since that
    lookback, a negative cell means it has decelerated/turned more negative.
    Complements build_global_output_growth_matrix_html's level/trajectory
    read with a direct "is growth speeding up or slowing down" read."""
    payload = json.loads(Path(market_data_json).read_text())
    gm = payload.get("global_macro", {})
    rows = []
    for country, indicators in gm.items():
        output = indicators.get("output_qoq", {})
        rows.append(_global_macro_row(country, output, cap=cap_pp, suffix="pp", invert=False, mode="delta"))
    return "\n".join(rows)


def build_yield_deep_dive_html(market_data_json: Path) -> str:
    """Native <details>/<summary> per tenor -- click to expand the full
    1W/1M/3M/1Y/3Y-ago-to-now value trail, no JS required."""
    payload = json.loads(Path(market_data_json).read_text())
    yc = payload.get("yield_curve", {})
    tenors, snapshots = yc.get("tenors", []), yc.get("snapshots", {})
    order = list(snapshots.keys())  # oldest -> "Now"
    blocks = []
    for t in tenors:
        now_v = snapshots.get("Now", {}).get(t)
        trail_rows = []
        for label in order:
            v = snapshots.get(label, {}).get(t)
            trail_rows.append(f'<tr><td class="metric">{label}</td><td class="lvl">{f"{v:.3f}%" if v is not None else "—"}</td></tr>')
        blocks.append(
            f'<details class="dd"><summary>{t} <span class="dd-now">{f"{now_v:.2f}%" if now_v is not None else "—"}</span></summary>'
            f'<table class="dd-table"><tbody>{"".join(trail_rows)}</tbody></table></details>'
        )
    return "\n".join(blocks)


def build_credit_deep_dive_html(market_data_json: Path) -> str:
    """Native <details>/<summary> per rating bucket, same idea as the yield
    deep-dive above."""
    payload = json.loads(Path(market_data_json).read_text())
    cc = payload.get("credit_curve", {})
    ratings, snapshots = cc.get("ratings", []), cc.get("snapshots", {})
    order = list(snapshots.keys())
    blocks = []
    for r in ratings:
        now_v = snapshots.get("Now", {}).get(r, {}).get("value_bp")
        trail_rows = []
        for label in order:
            info = snapshots.get(label, {}).get(r, {})
            v, as_of = info.get("value_bp"), info.get("as_of")
            v_str = f"{v:.0f}bp" if v is not None else "—"
            trail_rows.append(f'<tr><td class="metric">{label}</td><td class="lvl">{v_str}</td><td style="color:var(--ink-faint);font-size:11px;">{as_of or ""}</td></tr>')
        blocks.append(
            f'<details class="dd"><summary>{r} <span class="dd-now">{f"{now_v:.0f}bp" if now_v is not None else "—"}</span></summary>'
            f'<table class="dd-table"><tbody>{"".join(trail_rows)}</tbody></table></details>'
        )
    return "\n".join(blocks)


def build_labor_market_tiles_html(market_data_json: Path) -> str:
    """Macro-panel-style tiles for the US labor market: headline rates (with
    1M/1Y change), nonfarm payrolls (level + monthly change), initial claims
    (+ 4-week average), and wage growth -- same tile shape as
    build_macro_panel_html, one FRED-sourced number per tile with its own
    as-of date so staleness is visible."""
    payload = json.loads(Path(market_data_json).read_text())
    lm = payload.get("labor_market", {})
    tiles = []

    for sid, info in lm.get("rates", {}).items():
        if info.get("value") is None:
            continue
        chg = info.get("chg_1m")
        # A rise in the unemployment/underemployment rate is bad (red); a
        # rise in participation, openings, or quits is a healthy-labor-market
        # signal (green) -- opposite of the rate/spread convention above.
        bad_if_up = sid in ("UNRATE", "U6RATE")
        if not chg:
            color = "var(--ink-faint)"
        elif (chg > 0) == bad_if_up:
            color = "var(--down)"
        else:
            color = "var(--up)"
        chg_str = f"{'+' if (chg or 0) > 0 else ''}{chg} (1m)" if chg is not None else "—"
        tiles.append(
            '<div class="macro-tile">'
            f'<div class="k">{info["label"]}</div>'
            f'<div class="v">{info["value"]}%</div>'
            f'<div class="d" style="color:{color};">{chg_str}</div>'
            f'<div class="asof">FRED {sid} · {info["as_of"]}</div>'
            "</div>"
        )

    payrolls = lm.get("payrolls", {})
    if payrolls.get("chg_1m_k") is not None:
        chg = payrolls["chg_1m_k"]
        color = "var(--up)" if chg >= 0 else "var(--down)"
        tiles.append(
            '<div class="macro-tile">'
            '<div class="k">Nonfarm payrolls, monthly Δ</div>'
            f'<div class="v">{"+" if chg > 0 else ""}{chg:,.0f}k</div>'
            f'<div class="d" style="color:{color};">3M avg {payrolls.get("chg_3m_avg_k", 0):+,.0f}k</div>'
            f'<div class="asof">FRED PAYEMS · {payrolls["as_of"]}</div>'
            "</div>"
        )

    claims = lm.get("claims", {})
    if claims.get("value") is not None:
        chg = claims.get("chg_1m")
        color = "var(--down)" if (chg or 0) > 0 else "var(--up)"
        chg_str = f"{'+' if (chg or 0) > 0 else ''}{chg:,.0f} (1m)" if chg is not None else "—"
        tiles.append(
            '<div class="macro-tile">'
            '<div class="k">Initial jobless claims</div>'
            f'<div class="v">{claims["value"]:,.0f}</div>'
            f'<div class="d" style="color:{color};">{chg_str} · 4wk avg {claims["avg_4wk"]:,.0f}</div>'
            f'<div class="asof">FRED ICSA · {claims["as_of"]}</div>'
            "</div>"
        )

    wage = lm.get("wage_growth_yoy", {})
    if wage.get("value") is not None:
        tiles.append(
            '<div class="macro-tile">'
            '<div class="k">Wage growth, YoY</div>'
            f'<div class="v">{wage["value"]}%</div>'
            '<div class="d">Avg hourly earnings</div>'
            f'<div class="asof">FRED CES0500000003 · {wage["as_of"]}</div>'
            "</div>"
        )

    return "\n".join(tiles)


def build_payrolls_heatmap_html(market_data_json: Path, cap_k: float = 250.0) -> str:
    """Horizontal strip of color-coded cells, one per month of nonfarm payroll
    change (thousands) -- a compact 12-month heatmap of job creation, oldest
    to newest, using the same color-cell convention as every other matrix
    here (green = jobs added, red = jobs lost, intensity = magnitude)."""
    payload = json.loads(Path(market_data_json).read_text())
    history = payload.get("labor_market", {}).get("payrolls", {}).get("history", [])
    cells = []
    for row in history:
        month_label = row["month"][:7]  # YYYY-MM
        cells.append(
            _cell_html(row["chg_k"], cap=cap_k, suffix="k", title=f"{month_label}: {row['chg_k']:+.0f}k jobs")
            .replace('<td class="hm-cell"', f'<td class="hm-cell" data-month="{month_label}"')
        )
    header = "".join(f'<th>{row["month"][5:7]}/{row["month"][2:4]}</th>' for row in history)
    return (
        f'<table class="hm chg-matrix" style="min-width:0;">'
        f'<thead><tr>{header}</tr></thead>'
        f'<tbody><tr>{"".join(cells)}</tr></tbody>'
        f'</table>'
    )


def build_market_pulse_html(heatmap_csv: Path, periods: tuple = ("1D", "1W", "1M", "YTD"), top_n: int = 5) -> str:
    """Fully data-driven "what moved, at each horizon" scoreboard -- top N
    gainers/losers across the entire 32-ticker heatmap universe for each
    lookback window, each with a <details> "see top 10" deep dive. This is
    the quantitative half of the news-cadence view (day/week/month/YTD);
    the qualitative half is the News Pulse story log (render_dashboard.py)."""
    df = pd.read_csv(heatmap_csv)
    period_names = {"1D": "Today", "1W": "This week", "1M": "This month", "3M": "This quarter", "YTD": "Year to date"}
    blocks = []
    for period in periods:
        s = df[["ticker", "label", period]].dropna().sort_values(period)
        if s.empty:
            continue

        def chip_list(rows) -> str:
            items = []
            for _, r in rows.iterrows():
                token = "up" if r[period] >= 0 else "down"
                sign = "+" if r[period] > 0 else ""
                items.append(
                    f'<li><span class="chip {token}">{sign}{r[period]:.1f}%</span> '
                    f'<b>{r.ticker}</b> <small>{r.label}</small></li>'
                )
            return "".join(items)

        best5, worst5 = s.tail(top_n)[::-1], s.head(top_n)
        best10, worst10 = s.tail(10)[::-1], s.head(10)
        blocks.append(
            f'<div class="pulse-col"><h4>{period_names.get(period, period)}</h4>'
            f'<div class="pulse-sub">Best</div><ul class="pulse-list">{chip_list(best5)}</ul>'
            f'<div class="pulse-sub">Worst</div><ul class="pulse-list">{chip_list(worst5)}</ul>'
            f'<details class="dd"><summary>See top 10 each way</summary>'
            f'<div class="pulse-sub">Best 10</div><ul class="pulse-list">{chip_list(best10)}</ul>'
            f'<div class="pulse-sub">Worst 10</div><ul class="pulse-list">{chip_list(worst10)}</ul>'
            f'</details></div>'
        )
    return "\n".join(blocks)


def heatmap_highlights(heatmap_csv: Path, top_n: int = 3) -> dict:
    """Small numeric summary (best/worst YTD, best/worst 1D) usable to sanity-check
    or seed the narrative write-up, without hand-eyeballing the CSV."""
    df = pd.read_csv(heatmap_csv)
    out = {}
    for period in PERIOD_COLS:
        s = df[["ticker", "label", period]].dropna().sort_values(period)
        out[period] = {
            "worst": s.head(top_n)[["ticker", "label", period]].to_dict("records"),
            "best": s.tail(top_n)[["ticker", "label", period]].to_dict("records")[::-1],
        }
    return out
