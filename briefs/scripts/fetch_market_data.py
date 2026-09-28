#!/usr/bin/env python3
"""
Pull equity/factor/asset-class returns from Yahoo Finance and macro series from FRED
to complement the qualitative news brief with a quantitative snapshot.

Usage:
    python3 fetch_market_data.py [YYYY-MM-DD]

Output:
    briefs/data/<date>-market-data.json   -- full pull, machine-readable
    briefs/data/<date>-heatmap.csv        -- ticker x period return matrix, for the HTML heatmap

No API key required: equities via yfinance, macro series via FRED's public
fredgraph.csv endpoint (same data as the FRED website, no auth).
"""
import sys
import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]  # briefs/
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# ---- universe -------------------------------------------------------------

SECTORS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLY": "Cons. Discretionary", "XLP": "Cons. Staples", "XLI": "Industrials",
    "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Comm. Services",
}

FACTORS = {
    "MTUM": "Momentum", "VLUE": "Value", "QUAL": "Quality", "USMV": "Min Volatility",
    "SIZE": "Size (small)", "IWD": "Russell 1000 Value", "IWF": "Russell 1000 Growth",
}

ASSETS = {
    "SPY": "S&P 500", "QQQ": "Nasdaq 100", "DIA": "Dow 30", "IWM": "Russell 2000",
    "EFA": "Developed ex-US", "EEM": "Emerging Mkts", "VNQ": "US REITs",
    "TLT": "20+Y Treasury", "IEF": "7-10Y Treasury", "HYG": "High Yield Credit",
    "LQD": "IG Credit", "GLD": "Gold", "USO": "Oil (USO)", "UUP": "US Dollar (UUP)",
}

ALL_TICKERS = {**SECTORS, **FACTORS, **ASSETS}

FRED_SERIES = {
    "DGS2": "2Y Treasury Yield", "DGS10": "10Y Treasury Yield", "DGS30": "30Y Treasury Yield",
    "T10Y2Y": "10Y-2Y Spread", "BAMLH0A0HYM2": "HY OAS", "BAMLC0A0CM": "IG OAS",
    "DTWEXBGS": "Broad Dollar Index", "VIXCLS": "VIX",
}

PERIODS = {"1D": 2, "1W": 6, "1M": 23, "3M": 64, "YTD": None}

# ---- fixed income duration & credit-curve matrix ---------------------------
# Duration-bucketed Treasury/credit ETFs used for the duration matrix. Not in
# the returns heatmap universe above (that's sector/factor/broad-asset), so
# pulled separately with their own labels.
FI_MATRIX_TICKERS = {
    "SHY": "1-3Y Treasury", "IEI": "3-7Y Treasury", "IEF": "7-10Y Treasury",
    "TLT": "20+Y Treasury", "LQD": "IG Corporate", "HYG": "High Yield Corporate",
    "EMB": "EM USD Debt", "MUB": "Municipal",
}

# Approximate effective duration (years), per issuer fact sheets. These are
# fund characteristics, not day-specific market data -- static like the
# ticker->label maps above, reviewed occasionally rather than pulled live
# (no free API exposes ETF effective duration).
FI_DURATION_YEARS = {
    "SHY": 1.9, "IEI": 4.4, "IEF": 7.5, "TLT": 16.8,
    "LQD": 8.4, "HYG": 3.2, "EMB": 6.9, "MUB": 6.2,
}

# ICE BofA option-adjusted spread by rating bucket, in basis points -- builds
# the credit curve (spread vs. rating) rather than just IG-vs-HY.
CREDIT_CURVE_SERIES = {
    "AAA": "BAMLC0A1CAAA", "AA": "BAMLC0A2CAA", "A": "BAMLC0A3CA", "BBB": "BAMLC0A4CBBB",
    "BB": "BAMLH0A1HYBB", "B": "BAMLH0A2HYB", "CCC": "BAMLH0A3HYC",
}

# Full Treasury tenor set (vs. the 2Y/10Y/30Y trio in FRED_SERIES above) for
# the yield-curve-evolution chart -- how the whole curve, not just three
# points on it, has shifted over the last week/month/quarter/year(s).
TREASURY_TENORS = {
    "DGS1MO": "1M", "DGS3MO": "3M", "DGS6MO": "6M", "DGS1": "1Y", "DGS2": "2Y",
    "DGS3": "3Y", "DGS5": "5Y", "DGS7": "7Y", "DGS10": "10Y", "DGS20": "20Y", "DGS30": "30Y",
}

# Lookback snapshots for the yield-curve and credit-curve evolution charts,
# oldest first (drawing order: oldest line first, "Now" drawn last/on top).
CURVE_SNAPSHOT_OFFSETS = {
    "3Y ago": 1095, "1Y ago": 365, "3M ago": 91, "1M ago": 30, "1W ago": 7, "Now": 0,
}

# ---- cross-country macro matrix ---------------------------------------------
# Seven major-economy central banks, each with a policy-rate proxy (OECD
# "immediate rates" -- interbank/call money, the closest free FRED series to
# the official policy rate for non-US banks), CPI YoY, and GDP/consumption
# QoQ growth (OECD quarterly national accounts, seasonally adjusted, already
# expressed as growth-over-previous-period so no re-basing is needed).
GLOBAL_MACRO_COUNTRIES = {
    "US": {
        "rate": "IRSTCI01USM156N", "cpi_yoy": "CPALTT01USM659N",
        "output_qoq": "NAEXKP01USQ657S", "consumption_qoq": "NAEXKP02USQ657S",
    },
    "Euro Area": {
        "rate": "IRSTCI01EZM156N", "cpi_yoy": "CPHPTT01EZM659N",
        "output_qoq": "NAEXKP01EZQ657S", "consumption_qoq": "NAEXKP02EZQ657S",
    },
    "UK": {
        "rate": "IRSTCI01GBM156N", "cpi_yoy": "CPALTT01GBM659N",
        "output_qoq": "NAEXKP01GBQ657S", "consumption_qoq": "NAEXKP02GBQ657S",
    },
    "Japan": {
        "rate": "IRSTCI01JPM156N", "cpi_yoy": "CPALTT01JPM659N",
        "output_qoq": "NAEXKP01JPQ657S", "consumption_qoq": "NAEXKP02JPQ657S",
    },
    "Canada": {
        "rate": "IRSTCI01CAM156N", "cpi_yoy": "CPALTT01CAM659N",
        "output_qoq": "NAEXKP01CAQ657S", "consumption_qoq": "NAEXKP02CAQ657S",
    },
    "Australia": {
        "rate": "IRSTCI01AUM156N", "cpi_yoy": "CPALTT01AUQ659N",
        "output_qoq": "NAEXKP01AUQ657S", "consumption_qoq": "NAEXKP02AUQ657S",
    },
    "Switzerland": {
        "rate": "IRSTCI01CHM156N", "cpi_yoy": "CPALTT01CHM659N",
        "output_qoq": "NAEXKP01CHQ657S", "consumption_qoq": "NAEXKP02CHQ657S",
    },
}

# 1mo through 3yr, shortest first -- for rate/CPI this drives a "how much has
# it moved" delta matrix (like CURVE_SNAPSHOT_OFFSETS above); for output/
# consumption (already quarterly growth-rate series) it instead samples the
# growth print that prevailed at each point, i.e. a growth-trajectory
# heatmap. Quarterly series won't have fresh prints inside the 1M/2M window,
# so those columns legitimately repeat the same latest print -- that's
# correct, not a bug.
GLOBAL_MACRO_HORIZONS = {
    "1M": 30, "2M": 61, "3M": 91, "6M": 182, "12M": 365, "2Y": 730, "3Y": 1095,
}


GLOBAL_MACRO_HISTORY_MONTHS = 36


def _monthly_grid_history(s: pd.Series, grid: pd.DatetimeIndex) -> list:
    """Resample a FRED series onto a shared month-start grid (last-observation-
    per-month, forward-filled), so every country/indicator line in the trend
    charts sits on identical x-axis points -- no per-country date drift to
    reconcile client-side. Quarterly series correctly plateau between prints;
    months before the series' own history starts are simply omitted.

    The line STOPS at the month of the series' last real observation. Carrying
    a value forward *between* prints is legitimate (that value was the
    prevailing print); carrying it past the final print is not -- several of
    these OECD series lag by a year or more, and extending them to today draws
    a long flat line that reads as "inflation was constant" when it actually
    means "nobody has published since." Each point is tagged `filled` so the
    renderer can draw carried-forward segments differently from real prints."""
    # .resample("MS").last() leaves NaN in months a quarterly series doesn't
    # report in; .ffill() that *before* reindexing (not via reindex's own
    # method="ffill", which only looks at the nearest existing index label --
    # if that label's own value is one of those NaN placeholders, it returns
    # NaN instead of carrying the last real print forward).
    monthly = s.resample("MS").last()
    real_months = {d.strftime("%Y-%m") for d in monthly.dropna().index}
    aligned = monthly.ffill().reindex(grid, method="ffill")
    last_actual = s.index[-1]
    cutoff = pd.Timestamp(last_actual.year, last_actual.month, 1)
    return [
        {
            "date": str(d.date())[:7],
            "value": round(float(v), 3),
            "filled": str(d.date())[:7] not in real_months,
        }
        for d, v in aligned.items()
        if pd.notna(v) and d <= cutoff
    ]


def fetch_global_macro_matrix() -> dict:
    """Cross-country matrix: for each country/indicator, the latest value,
    its value at each GLOBAL_MACRO_HORIZONS lookback (drives the change/
    trajectory heatmaps), and a full trailing 36-month history on a shared
    monthly grid (drives the granular trend-line charts -- real monthly
    resolution, not just the 8 discrete horizon snapshots)."""
    print(f"Pulling global macro matrix for {len(GLOBAL_MACRO_COUNTRIES)} countries from FRED...")
    grid = pd.date_range(
        end=pd.Timestamp.today().normalize().replace(day=1), periods=GLOBAL_MACRO_HISTORY_MONTHS, freq="MS",
    )
    out = {}
    for country, indicators in GLOBAL_MACRO_COUNTRIES.items():
        out[country] = {}
        for indicator, sid in indicators.items():
            s = fetch_fred_series(sid)
            if s is None or s.empty:
                out[country][indicator] = {"series_id": sid, "asof": None, "now": None, "horizons": {}, "history": []}
                continue
            asof = s.index[-1]
            now_val = round(float(s.iloc[-1]), 3)
            horizons = {}
            for label, days in GLOBAL_MACRO_HORIZONS.items():
                target = asof - pd.Timedelta(days=days)
                window = s[s.index <= target]
                horizons[label] = round(float(window.iloc[-1]), 3) if not window.empty else None
            # How far behind today's date this series' last real print is.
            # Several OECD series lag badly; the dashboard labels that rather
            # than presenting a stale number as current.
            months_stale = round((pd.Timestamp.today().normalize() - asof).days / 30.44, 1)
            out[country][indicator] = {
                "series_id": sid, "asof": str(asof.date()), "now": now_val, "horizons": horizons,
                "months_stale": months_stale,
                "is_stale": months_stale >= 4.0,
                "history": _monthly_grid_history(s, grid),
            }
    return out


# ---- equity relative valuation ---------------------------------------------
# Sector SPDRs + SPY as the peer-group benchmark for relative (not absolute
# historical-percentile -- no free source for multi-year ETF P/E history)
# valuation: each sector's trailing/forward P/E vs. the peer-group average.
VALUATION_TICKERS = {**SECTORS, "SPY": "S&P 500 (benchmark)"}


def pct_return(series: pd.Series, lookback_days: Optional[int], asof: pd.Timestamp) -> Optional[float]:
    series = series.dropna()
    if series.empty:
        return None
    if lookback_days is None:  # YTD
        year_start = series[series.index >= pd.Timestamp(asof.year, 1, 1)]
        if year_start.empty:
            return None
        base = year_start.iloc[0]
    else:
        if len(series) <= lookback_days:
            base = series.iloc[0]
        else:
            base = series.iloc[-1 - lookback_days]
    last = series.iloc[-1]
    if base == 0 or pd.isna(base):
        return None
    return round(float((last / base - 1) * 100), 2)


def fetch_equity_returns(asof_str: str):
    tickers = list(ALL_TICKERS.keys())
    print(f"Pulling {len(tickers)} tickers from Yahoo Finance...")
    hist = yf.download(tickers, period="1y", progress=False, auto_adjust=True)["Close"]
    asof = pd.Timestamp(hist.index[-1])

    rows = {}
    for tkr, label in ALL_TICKERS.items():
        if tkr not in hist.columns:
            continue
        s = hist[tkr]
        rows[tkr] = {
            "label": label,
            "category": "sector" if tkr in SECTORS else ("factor" if tkr in FACTORS else "asset"),
            "level": round(float(s.dropna().iloc[-1]), 2),
            "returns": {p: pct_return(s, lb, asof) for p, lb in PERIODS.items()},
        }
    # Return the raw daily-price frame alongside the summary so callers that
    # need a real time series (the sector streamgraph) don't have to hit
    # Yahoo Finance a second time for tickers already pulled here.
    return {"as_of": str(asof.date()), "tickers": rows}, hist


def build_sector_streamgraph(hist: pd.DataFrame, months: int = 12) -> dict:
    """Monthly-resampled, indexed-to-100 price series per sector SPDR over
    the trailing `months` -- the streamgraph's raw material. Everything else
    in this file is a point-in-time snapshot (today, or today vs. N lookbacks
    ago); this is the one place with an actual walked time series, which is
    what a streamgraph needs to say something a static heatmap can't: how
    sector leadership rotated continuously, not just where it landed."""
    cols = [c for c in SECTORS if c in hist.columns]
    sector_hist = hist[cols].dropna(how="all")
    monthly = sector_hist.resample("ME").last()
    monthly = monthly.tail(months + 1)
    dates = [d.strftime("%b") for d in monthly.index]

    series = {}
    for c in cols:
        s = monthly[c]
        first_valid = s.first_valid_index()
        if first_valid is None:
            continue
        base = s.loc[first_valid]
        indexed = (s / base * 100)
        series[c] = [None if pd.isna(v) else round(float(v), 2) for v in indexed]
    return {"dates": dates, "labels": {c: SECTORS[c] for c in series}, "series": series}


def fetch_fred_series(series_id: str) -> Optional[pd.Series]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        df = pd.read_csv(url, parse_dates=["observation_date"])
    except Exception as e:
        print(f"  FRED pull failed for {series_id}: {e}")
        return None
    df = df.rename(columns={df.columns[1]: "value"})
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna().set_index("observation_date")["value"]
    return df


def fetch_fi_matrix(asof_str: str) -> dict:
    """Duration-bucketed Treasury/credit ETF returns, joined with static
    duration metadata, for the fixed-income duration matrix."""
    tickers = list(FI_MATRIX_TICKERS.keys())
    print(f"Pulling {len(tickers)} fixed-income tickers from Yahoo Finance...")
    try:
        hist = yf.download(tickers, period="1y", progress=False, auto_adjust=True)["Close"]
    except Exception as e:
        print(f"  FI matrix pull failed: {e}")
        return {}
    asof = pd.Timestamp(hist.index[-1])

    rows = {}
    for tkr, label in FI_MATRIX_TICKERS.items():
        if tkr not in hist.columns:
            continue
        s = hist[tkr]
        rows[tkr] = {
            "label": label,
            "duration_years": FI_DURATION_YEARS.get(tkr),
            "level": round(float(s.dropna().iloc[-1]), 2),
            "returns": {p: pct_return(s, lb, asof) for p, lb in PERIODS.items()},
        }
    return rows


def snapshot_series(s: pd.Series, asof: pd.Timestamp, offsets_days: dict) -> dict:
    """Sample a FRED time series at asof minus each named offset, using the
    last available observation on or before that date (handles weekends/
    holidays without interpolating)."""
    out = {}
    for label, days in offsets_days.items():
        target = asof - pd.Timedelta(days=days)
        window = s[s.index <= target]
        if window.empty:
            out[label] = {"value": None, "as_of": None}
        else:
            out[label] = {"value": round(float(window.iloc[-1]), 4), "as_of": str(window.index[-1].date())}
    return out


def fetch_credit_curve_history() -> dict:
    """OAS by rating bucket (AAA..CCC), each sampled at today plus the
    1W/1M/3M/1Y/3Y-ago snapshots -- the credit curve's shape *and* how it has
    moved, not just today's snapshot."""
    print(f"Pulling {len(CREDIT_CURVE_SERIES)} credit-curve series (with history) from FRED...")
    rating_series = {}
    for rating, sid in CREDIT_CURVE_SERIES.items():
        s = fetch_fred_series(sid)
        if s is not None and not s.empty:
            rating_series[rating] = s
    if not rating_series:
        return {}

    asof = max(s.index[-1] for s in rating_series.values())
    order = [r for r in ("AAA", "AA", "A", "BBB", "BB", "B", "CCC") if r in rating_series]
    snapshots = {}
    for label, days in CURVE_SNAPSHOT_OFFSETS.items():
        raw = {r: snapshot_series(rating_series[r], asof, {label: days})[label] for r in order}
        # FRED reports these OAS series in percent (e.g. 2.73 = 273bp); convert
        # to bp to match how spreads are quoted elsewhere in the brief.
        snapshots[label] = {
            r: {"value_bp": round(v["value"] * 100, 1) if v["value"] is not None else None, "as_of": v["as_of"]}
            for r, v in raw.items()
        }
    history_start = str(min(s.index[0] for s in rating_series.values()).date())

    # Monthly-resampled full history (bp) for the credit-curve streamgraph --
    # the 6-point snapshots above answer "how much has it moved"; this is the
    # continuous walk between those points, over however much history this
    # FRED mirror actually has for these series (see history_start).
    df = pd.DataFrame({r: rating_series[r] for r in order}).sort_index()
    monthly = (df * 100).resample("ME").last()
    stream = {
        "dates": [d.strftime("%b '%y") for d in monthly.index],
        "series": {r: [None if pd.isna(v) else round(float(v), 1) for v in monthly[r]] for r in order},
    }

    return {
        "asof": str(asof.date()), "ratings": order, "snapshots": snapshots,
        "history_start": history_start, "stream": stream,
    }


def fetch_yield_curve_history() -> dict:
    """Full 11-tenor Treasury curve, sampled at today plus the
    1W/1M/3M/1Y/3Y-ago snapshots -- lets the dashboard draw how the *whole*
    curve has moved (twist, steepen, flatten), not just the 2s10s spread."""
    print(f"Pulling {len(TREASURY_TENORS)}-tenor yield curve (with history) from FRED...")
    tenor_series = {}
    for sid, tenor in TREASURY_TENORS.items():
        s = fetch_fred_series(sid)
        if s is not None and not s.empty:
            tenor_series[tenor] = s
    if not tenor_series:
        return {}

    asof = max(s.index[-1] for s in tenor_series.values())
    tenors = [t for t in TREASURY_TENORS.values() if t in tenor_series]
    snapshots = {}
    for label, days in CURVE_SNAPSHOT_OFFSETS.items():
        snapshots[label] = {
            t: snapshot_series(tenor_series[t], asof, {label: days})[label]["value"] for t in tenors
        }
    history_start = str(min(s.index[0] for s in tenor_series.values()).date())
    return {"asof": str(asof.date()), "tenors": tenors, "snapshots": snapshots, "history_start": history_start}


def fetch_valuation() -> dict:
    """Trailing/forward P/E, P/B, dividend yield per sector SPDR + SPY, plus
    each sector's valuation relative to the SPY benchmark and to the
    sector-peer average (z-score) -- a relative-valuation cross-section, not
    a historical-percentile one (no free multi-year ETF P/E history exists)."""
    print(f"Pulling valuation multiples for {len(VALUATION_TICKERS)} tickers...")
    raw = {}
    for tkr in VALUATION_TICKERS:
        try:
            info = yf.Ticker(tkr).info
        except Exception as e:
            print(f"  valuation pull failed for {tkr}: {e}")
            info = {}
        raw[tkr] = {
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "price_to_book": info.get("priceToBook"),
            "dividend_yield": info.get("dividendYield"),
        }

    spy_pe = raw.get("SPY", {}).get("trailing_pe")
    sector_pes = [raw[t]["trailing_pe"] for t in SECTORS if raw.get(t, {}).get("trailing_pe")]
    peer_mean = sum(sector_pes) / len(sector_pes) if sector_pes else None
    peer_std = (
        (sum((x - peer_mean) ** 2 for x in sector_pes) / len(sector_pes)) ** 0.5
        if peer_mean and len(sector_pes) > 1 else None
    )

    out = {}
    for tkr, label in VALUATION_TICKERS.items():
        pe = raw[tkr]["trailing_pe"]
        out[tkr] = {
            "label": label,
            **raw[tkr],
            "premium_to_spy_pct": round((pe / spy_pe - 1) * 100, 1) if pe and spy_pe else None,
            "peer_zscore": round((pe - peer_mean) / peer_std, 2) if pe and peer_mean and peer_std else None,
        }
    out["_peer_group"] = {"mean_pe": round(peer_mean, 1) if peer_mean else None, "n": len(sector_pes)}
    return out


def fetch_macro() -> dict:
    print(f"Pulling {len(FRED_SERIES)} series from FRED...")
    out = {}
    for sid, label in FRED_SERIES.items():
        s = fetch_fred_series(sid)
        if s is None or s.empty:
            out[sid] = {"label": label, "value": None, "as_of": None, "chg_1m": None}
            continue
        latest = s.iloc[-1]
        asof = s.index[-1]
        month_ago = s[s.index <= asof - pd.Timedelta(days=30)]
        chg = round(float(latest - month_ago.iloc[-1]), 3) if not month_ago.empty else None
        out[sid] = {
            "label": label,
            "value": round(float(latest), 3),
            "as_of": str(asof.date()),
            "chg_1m": chg,
        }
    return out


# ---- US labor market ---------------------------------------------------
# Headline-rate series (level + 1M/1Y change, same shape as fetch_macro's
# FRED_SERIES) plus two series that need their own transform: PAYEMS is a
# level (thousands of jobs) so the useful number is its month-over-month
# *change*, and ICSA is a noisy weekly print so the useful number is its
# 4-week average.
LABOR_RATE_SERIES = {
    "UNRATE": "Unemployment rate", "U6RATE": "U-6 (underemployment)",
    "CIVPART": "Labor force participation", "JTSJOR": "Job openings rate",
    "JTSQUR": "Quits rate",
}


def fetch_labor_market() -> dict:
    """US labor market snapshot: headline rates (with 1M/1Y change), nonfarm
    payrolls (level + monthly change history for the payrolls heatmap strip),
    initial claims (level + 4-week average), and wage growth (YoY, from the
    average-hourly-earnings level series)."""
    print(f"Pulling US labor market data ({len(LABOR_RATE_SERIES) + 3} series) from FRED...")
    out = {"rates": {}}
    for sid, label in LABOR_RATE_SERIES.items():
        s = fetch_fred_series(sid)
        if s is None or s.empty:
            out["rates"][sid] = {"label": label, "value": None, "as_of": None, "chg_1m": None, "chg_1y": None}
            continue
        latest, asof = s.iloc[-1], s.index[-1]
        month_ago = s[s.index <= asof - pd.Timedelta(days=30)]
        year_ago = s[s.index <= asof - pd.Timedelta(days=365)]
        out["rates"][sid] = {
            "label": label,
            "value": round(float(latest), 2),
            "as_of": str(asof.date()),
            "chg_1m": round(float(latest - month_ago.iloc[-1]), 2) if not month_ago.empty else None,
            "chg_1y": round(float(latest - year_ago.iloc[-1]), 2) if not year_ago.empty else None,
        }

    payems = fetch_fred_series("PAYEMS")
    if payems is not None and not payems.empty:
        diffs = payems.diff().dropna()
        last12 = diffs.tail(12)
        out["payrolls"] = {
            "level_k": round(float(payems.iloc[-1]), 0),
            "as_of": str(payems.index[-1].date()),
            "chg_1m_k": round(float(diffs.iloc[-1]), 0) if not diffs.empty else None,
            "chg_3m_avg_k": round(float(diffs.tail(3).mean()), 0) if len(diffs) >= 3 else None,
            "history": [
                {"month": str(d.date()), "chg_k": round(float(v), 0)} for d, v in last12.items()
            ],
        }
    else:
        out["payrolls"] = {}

    claims = fetch_fred_series("ICSA")
    if claims is not None and not claims.empty:
        month_ago = claims[claims.index <= claims.index[-1] - pd.Timedelta(days=30)]
        out["claims"] = {
            "value": round(float(claims.iloc[-1]), 0),
            "as_of": str(claims.index[-1].date()),
            "avg_4wk": round(float(claims.tail(4).mean()), 0),
            "chg_1m": round(float(claims.iloc[-1] - month_ago.iloc[-1]), 0) if not month_ago.empty else None,
        }
    else:
        out["claims"] = {}

    ahe = fetch_fred_series("CES0500000003")
    if ahe is not None and not ahe.empty:
        asof = ahe.index[-1]
        year_ago = ahe[ahe.index <= asof - pd.Timedelta(days=365)]
        yoy = round(float((ahe.iloc[-1] / year_ago.iloc[-1] - 1) * 100), 2) if not year_ago.empty else None
        out["wage_growth_yoy"] = {"value": yoy, "as_of": str(asof.date())}
    else:
        out["wage_growth_yoy"] = {}

    return out


def main():
    asof_str = sys.argv[1] if len(sys.argv) > 1 else str(date.today())

    equities, equities_hist = fetch_equity_returns(asof_str)
    macro = fetch_macro()
    fi_matrix = fetch_fi_matrix(asof_str)
    credit_curve = fetch_credit_curve_history()
    yield_curve = fetch_yield_curve_history()
    valuation = fetch_valuation()
    global_macro = fetch_global_macro_matrix()
    labor_market = fetch_labor_market()
    sector_streamgraph = build_sector_streamgraph(equities_hist)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "equities": equities,
        "macro": macro,
        "fi_matrix": fi_matrix,
        "credit_curve": credit_curve,
        "yield_curve": yield_curve,
        "valuation": valuation,
        "global_macro": global_macro,
        "labor_market": labor_market,
        "sector_streamgraph": sector_streamgraph,
    }

    out_json = DATA_DIR / f"{asof_str}-market-data.json"
    out_json.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out_json}")

    # heatmap CSV: ticker, category, label, 1D, 1W, 1M, 3M, YTD
    heat_rows = []
    for tkr, info in equities["tickers"].items():
        row = {"ticker": tkr, "category": info["category"], "label": info["label"]}
        row.update(info["returns"])
        heat_rows.append(row)
    heat_df = pd.DataFrame(heat_rows).sort_values(["category", "ticker"])
    out_csv = DATA_DIR / f"{asof_str}-heatmap.csv"
    heat_df.to_csv(out_csv, index=False)
    print(f"Wrote {out_csv}")

    print("\n--- Heatmap preview ---")
    print(heat_df.to_string(index=False))
    print("\n--- Macro snapshot ---")
    for sid, info in macro.items():
        print(f"{sid:14s} {info['label']:22s} {info['value']}  (as of {info['as_of']}, 1m chg {info['chg_1m']})")

    print("\n--- Credit curve (OAS by rating, latest) ---")
    if credit_curve:
        for rating in credit_curve["ratings"]:
            now = credit_curve["snapshots"]["Now"][rating]
            print(f"{rating:5s} {now['value_bp']}bp  (as of {now['as_of']})")

    print("\n--- Yield curve (latest) ---")
    if yield_curve:
        for tenor in yield_curve["tenors"]:
            print(f"{tenor:5s} {yield_curve['snapshots']['Now'][tenor]}%")

    print("\n--- Valuation (trailing P/E, peer z-score) ---")
    for tkr, info in valuation.items():
        if tkr == "_peer_group":
            continue
        print(f"{tkr:6s} {info['label']:22s} PE={info['trailing_pe']}  z={info['peer_zscore']}")

    print("\n--- Global macro matrix (policy rate, latest) ---")
    for country, indicators in global_macro.items():
        rate = indicators.get("rate", {})
        print(f"{country:12s} rate={rate.get('now')}  (as of {rate.get('asof')})")


if __name__ == "__main__":
    main()
