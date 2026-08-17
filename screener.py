#!/usr/bin/env python3
"""
Panora — PEA-eligible screener across Euronext Paris, Amsterdam, Brussels, Lisbon.

All thresholds, weights, and filters live in config.json. This file is pure
mechanics.

Two stages, needed to run on a universe of 1000+ names without hitting Yahoo's
rate limits:
  Stage 1 — prices only, downloaded in batches. Trend, liquidity, MA200.
            Most of the universe gets cut here.
  Stage 2 — fundamentals, one call per ticker, only for survivors.
"""

from __future__ import annotations

import json
import math
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text())
UNIVERSE = ROOT / "universe.csv"
OUTPUT = ROOT / "data.json"

U, FLT, W = CFG["universe"], CFG["elimination_filters"], CFG["score_weights"]
GT, GQ, GG, GV = (CFG["trend_grid"], CFG["quality_grid"],
                  CFG["growth_grid"], CFG["value_grid"])
HP, EX, THR = CFG["half_price_5y_valuation"], CFG["execution"], CFG["display_thresholds"]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def safe(x, default=None):
    try:
        if x is None:
            return default
        v = float(x)
        return default if (math.isnan(v) or math.isinf(v)) else v
    except (TypeError, ValueError):
        return default


def row_value(df, keys, col=0):
    if df is None or df.empty or col >= len(df.columns):
        return None
    for k in keys:
        if k in df.index:
            return safe(df.loc[k].iloc[col])
    return None


def cagr(first, last, years):
    if not first or not last or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def rnd(v, d):
    return round(v, d) if v is not None else None


# --------------------------------------------------------------------------
# Stage 1 — trend and liquidity
# --------------------------------------------------------------------------

def compute_trend(hist, bench):
    if hist is None or len(hist) < 220:
        return None
    close = hist["Close"].dropna()
    if len(close) < 220:
        return None

    price = float(close.iloc[-1])
    ma50 = float(close.rolling(50).mean().iloc[-1])
    ma200_s = close.rolling(200).mean().dropna()
    ma200 = float(ma200_s.iloc[-1])

    ref = ma200_s.iloc[-127] if len(ma200_s) > 126 else ma200_s.iloc[0]
    slope_6m = ma200 / float(ref) - 1

    aligned = close.loc[ma200_s.index]
    days_below = 0
    for below in (aligned < ma200_s).iloc[::-1]:
        if below:
            days_below += 1
        else:
            break

    last_252 = close.iloc[-252:] if len(close) >= 252 else close
    perf_52w = price / float(last_252.iloc[0]) - 1
    dist_high = price / float(last_252.max()) - 1

    weekly = close.resample("W").last().dropna()
    delta = weekly.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi_w = safe((100 - 100 / (1 + gain / loss.replace(0, pd.NA))).iloc[-1], 50)

    rel = None
    if bench is not None and len(bench) > 252:
        b = bench.iloc[-252:]
        rel = perf_52w - (float(b.iloc[-1]) / float(b.iloc[0]) - 1)

    # Liquidity: median daily euro turnover over the last ~3 months
    turnover = None
    if "Volume" in hist:
        vol = hist["Volume"].dropna().iloc[-63:]
        if len(vol) > 20:
            turnover = float((vol * close.iloc[-63:]).median())

    return {
        "price": round(price, 2), "ma50": round(ma50, 2), "ma200": round(ma200, 2),
        "ma200_slope_6m": round(slope_6m, 4), "days_below_ma200": days_below,
        "perf_52w": round(perf_52w, 4), "dist_high_52w": round(dist_high, 4),
        "rsi_weekly": round(rsi_w, 1),
        "rel_strength_52w": rnd(rel, 4),
        "turnover_median": rnd(turnover, 0),
        "above_ma50": price > ma50, "above_ma200": price > ma200,
    }


# --------------------------------------------------------------------------
# Stage 2 — fundamentals
# --------------------------------------------------------------------------

EMPTY_FUND = {k: None for k in (
    "name", "country", "industry", "market_cap", "pe", "peg", "roe", "roce",
    "net_margin", "ev_ebitda", "net_debt_ebitda", "net_debt_net_income", "fcf_yield",
    "current_ratio", "rev_growth_3y", "eps_growth_3y", "insider_pct", "dividend_yield",
    "fair_value_5y", "cagr_implied_5y")}
EMPTY_FUND.update({"half_price_pass": False, "net_income_positive": None,
                   "fcf_positive": None, "fundamentals_available": False})


def compute_fundamentals(ticker, price):
    tk = yf.Ticker(ticker)
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    try:
        inc, bal, cfs = tk.income_stmt, tk.balance_sheet, tk.cashflow
    except Exception:
        inc = bal = cfs = pd.DataFrame()

    mcap = safe(info.get("marketCap"))
    shares = safe(info.get("sharesOutstanding"))

    net_income = row_value(inc, ["Net Income", "Net Income Common Stockholders"])
    revenue = row_value(inc, ["Total Revenue", "Operating Revenue"])
    ebit = row_value(inc, ["EBIT", "Operating Income"])
    ebitda = row_value(inc, ["EBITDA", "Normalized EBITDA"]) or safe(info.get("ebitda"))

    equity = row_value(bal, ["Stockholders Equity", "Common Stock Equity",
                             "Total Equity Gross Minority Interest"])
    assets = row_value(bal, ["Total Assets"])
    cl = row_value(bal, ["Current Liabilities", "Total Current Liabilities"])
    ca = row_value(bal, ["Current Assets", "Total Current Assets"])
    debt = row_value(bal, ["Total Debt"]) or safe(info.get("totalDebt"))
    cash = row_value(bal, ["Cash And Cash Equivalents",
                           "Cash Cash Equivalents And Short Term Investments"]) or safe(info.get("totalCash"))

    ocf = row_value(cfs, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"])
    capex = row_value(cfs, ["Capital Expenditure"])
    fcf = safe(info.get("freeCashflow"))
    if fcf is None and ocf is not None and capex is not None:
        fcf = ocf + capex

    net_debt = (debt - cash) if (debt is not None and cash is not None) else None

    def r(a, b):
        return None if (a is None or b in (None, 0)) else a / b

    roe, net_margin = r(net_income, equity), r(net_income, revenue)
    roce = r(ebit, (assets - cl) if (assets and cl) else None)
    ev = (mcap + net_debt) if (mcap is not None and net_debt is not None) else safe(info.get("enterpriseValue"))
    current_ratio = r(ca, cl) or safe(info.get("currentRatio"))

    rev_growth = eps_growth = None
    if inc is not None and not inc.empty and len(inc.columns) >= 3:
        n = min(4, len(inc.columns)) - 1
        rev_growth = cagr(row_value(inc, ["Total Revenue", "Operating Revenue"], n), revenue, n)
        ni_old = row_value(inc, ["Net Income", "Net Income Common Stockholders"], n)
        if ni_old and net_income and shares:
            eps_growth = cagr(ni_old / shares, net_income / shares, n)

    eps = safe(info.get("trailingEps"))
    pe = r(price, eps) if (eps and eps > 0) else safe(info.get("trailingPE"))
    peg = (pe / (eps_growth * 100)) if (pe and eps_growth and eps_growth > 0) else None

    cagr_implied = fair_5y = None
    if eps and eps > 0 and price:
        g = min(eps_growth if eps_growth else HP["eps_growth_default"], HP["eps_growth_cap"])
        exit_pe = min(pe if pe else 15, HP["exit_pe_cap"])
        fair_5y = eps * ((1 + g) ** 5) * exit_pe
        cagr_implied = (fair_5y / price) ** 0.2 - 1

    return {
        "name": info.get("longName") or info.get("shortName"),
        "country": info.get("country"),
        "industry": info.get("sector") or info.get("industry"),
        "market_cap": mcap,
        "pe": rnd(pe, 1), "peg": rnd(peg, 2),
        "roe": rnd(roe, 4), "roce": rnd(roce, 4), "net_margin": rnd(net_margin, 4),
        "ev_ebitda": rnd(r(ev, ebitda), 1),
        "net_debt_ebitda": rnd(r(net_debt, ebitda), 2),
        "net_debt_net_income": rnd(r(net_debt, net_income), 2),
        "fcf_yield": rnd(r(fcf, mcap), 4),
        "current_ratio": rnd(current_ratio, 2),
        "rev_growth_3y": rnd(rev_growth, 4), "eps_growth_3y": rnd(eps_growth, 4),
        "insider_pct": rnd(safe(info.get("heldPercentInsiders"), 0), 4),
        "dividend_yield": rnd(safe(info.get("dividendYield"), 0), 4),
        "fair_value_5y": rnd(fair_5y, 2), "cagr_implied_5y": rnd(cagr_implied, 4),
        "half_price_pass": bool(cagr_implied is not None and cagr_implied >= HP["target_cagr"]),
        "net_income_positive": bool(net_income and net_income > 0) if net_income is not None else None,
        "fcf_positive": bool(fcf and fcf > 0) if fcf is not None else None,
        "fundamentals_available": bool(revenue or net_income),
    }


# --------------------------------------------------------------------------
# Filters and scoring
# --------------------------------------------------------------------------

def trend_filter(t):
    out = []
    if not FLT["enabled"]:
        return out
    if t["days_below_ma200"] > FLT["max_days_below_ma200"]:
        out.append(f"Below MA200 for {t['days_below_ma200']} sessions")
    if t["turnover_median"] is not None and t["turnover_median"] < U["min_daily_turnover_eur"]:
        out.append(f"Liquidity €{t['turnover_median']/1000:.0f}k/day")
    return out


def fundamental_filter(f, sector):
    out = []
    if not FLT["enabled"] or not f["fundamentals_available"]:
        return out
    if f["market_cap"] is not None and f["market_cap"] < U["min_market_cap_eur"]:
        out.append(f"Market cap €{f['market_cap']/1e6:.0f}M")
    if FLT["net_income_must_be_positive"] and f["net_income_positive"] is False:
        out.append("Negative net income")
    if FLT["fcf_must_be_positive"] and f["fcf_positive"] is False:
        out.append("Negative FCF")
    nd = f["net_debt_ebitda"]
    if nd is not None and nd > FLT["max_net_debt_to_ebitda"] and sector not in FLT["sectors_exempt_from_debt_filter"]:
        out.append(f"Net debt/EBITDA {nd:.1f}x")
    cr = f["current_ratio"]
    if cr is not None and cr < FLT["min_current_ratio"] and sector not in FLT["sectors_exempt_from_debt_filter"]:
        out.append(f"Current ratio {cr:.2f}")
    g = f["rev_growth_3y"]
    if g is not None and g < FLT["min_revenue_growth_3y"]:
        out.append("Revenue declining over 3 years")
    return out


def score_trend(t):
    up = t["ma200_slope_6m"] > GT["min_ma200_slope"]
    if (t["above_ma50"] and t["above_ma200"] and up
            and t["perf_52w"] > GT["great_52w_perf"]
            and (t["rel_strength_52w"] or 0) > GT["great_relative_strength"]):
        return 5
    if t["above_ma50"] and t["above_ma200"] and up and t["perf_52w"] > GT["good_52w_perf"]:
        return 4
    if t["above_ma200"] and t["ma200_slope_6m"] > -0.02:
        return 3
    if t["above_ma200"]:
        return 2
    return 1


def score_quality(f):
    roe, nd, fy = f["roe"], f["net_debt_ebitda"], f["fcf_yield"]
    if roe is None:
        return 2
    if (roe > GQ["excellent_roe"] and (nd is None or nd < GQ["excellent_debt_to_ebitda"])
            and (fy or 0) > GQ["excellent_fcf_yield"]):
        return 5
    if roe > GQ["good_roe"] and (nd is None or nd < GQ["acceptable_debt_to_ebitda"]):
        return 4
    if roe > GQ["fair_roe"]:
        return 3
    if roe > GQ["weak_roe"]:
        return 2
    return 1


def score_growth(f):
    g = f["rev_growth_3y"]
    if g is None:
        return 2
    if g > GG["great_revenue_growth"]:
        return 5
    if g > GG["good_revenue_growth"]:
        return 4
    if g > GG["fair_revenue_growth"]:
        return 3
    return 2 if g > 0 else 1


def score_value(f):
    pe, fy, ev = f["pe"], f["fcf_yield"], f["ev_ebitda"]
    if (pe and pe < GV["excellent_pe"] and (fy or 0) > GV["excellent_fcf_yield"]
            and ev and ev < GV["excellent_ev_ebitda"]):
        return 5
    if pe and pe < GV["good_pe"] and (fy or 0) > GV["good_fcf_yield"]:
        return 4
    if pe and pe < GV["fair_pe"]:
        return 3
    if pe and pe < GV["high_pe"]:
        return 2
    return 1


def global_score(t, f):
    s = {"trend": score_trend(t), "quality": score_quality(f),
         "growth": score_growth(f), "value": score_value(f)}
    s["global"] = round(s["trend"] * W["trend"] + s["quality"] * W["quality"]
                        + s["growth"] * W["growth"] + s["value"] * W["value"], 2)
    return s


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def download_prices(tickers, chunk=120):
    frames = {}
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            d = yf.download(part, period="2y", interval="1d", group_by="ticker",
                            progress=False, auto_adjust=True, threads=True)
        except Exception as e:
            print(f"  Batch {i//chunk + 1} failed: {str(e)[:80]}")
            continue
        for t in part:
            try:
                sub = d[t].dropna(how="all") if len(part) > 1 else d.dropna(how="all")
                if not sub.empty:
                    frames[t] = sub
            except Exception:
                pass
        print(f"  Prices: {len(frames)} series fetched out of {min(i+chunk, len(tickers))} requested")
        time.sleep(1)
    return frames


def main():
    uni = pd.read_csv(UNIVERSE)
    for col in ("sector", "name", "country", "isin"):
        if col not in uni:
            uni[col] = ""
        uni[col] = uni[col].fillna("")
    tickers = uni["ticker"].tolist()
    print(f"Universe: {len(tickers)} names")

    bench = None
    for b in (EX["benchmark"], EX["benchmark_fallback"]):
        try:
            d = yf.download(b, period="2y", interval="1d", progress=False, auto_adjust=True)
            if not d.empty:
                bench = d["Close"].squeeze()
                print(f"Benchmark: {b}")
                break
        except Exception:
            continue

    print("\nStage 1 — price and trend")
    prices = download_prices(tickers)

    stage1, errors = [], []
    for _, m in uni.iterrows():
        t = compute_trend(prices.get(m["ticker"]), bench)
        if t is None:
            errors.append({"ticker": m["ticker"], "reason": "insufficient price history"})
            continue
        stage1.append({"meta": m, "trend": t, "excl": trend_filter(t)})

    n_passed = sum(1 for x in stage1 if not x["excl"])
    print(f"  {len(stage1)} names with data, {n_passed} pass the trend filter")

    print("\nStage 2 — fundamentals")
    rows = []
    for n, x in enumerate(stage1, 1):
        m, t, excl = x["meta"], x["trend"], x["excl"]
        need_fundamentals = (not excl) or (not EX["fundamentals_only_if_trend_passes"])
        if need_fundamentals:
            try:
                f = compute_fundamentals(m["ticker"], t["price"])
            except Exception as e:
                f = dict(EMPTY_FUND)
                errors.append({"ticker": m["ticker"], "reason": str(e)[:100]})
            time.sleep(EX["pause_between_calls_sec"])
        else:
            f = dict(EMPTY_FUND)

        sector = m["sector"] or f["industry"] or "Unclassified"
        reasons = excl + fundamental_filter(f, sector)
        rows.append({
            "ticker": m["ticker"], "name": f["name"] or m["name"] or m["ticker"],
            "market": m["market"], "sector": sector,
            "country": f["country"] or m["country"], "isin": m["isin"],
            "excluded": bool(reasons), "reasons": reasons,
            **t, **{k: v for k, v in f.items() if k not in ("name", "country", "industry")},
            "scores": global_score(t, f),
        })
        if n % 50 == 0:
            print(f"  {n}/{len(stage1)}")

    kept = [r for r in rows if not r["excluded"]]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": CFG,
        "funnel": {
            "universe": len(tickers),
            "data_ok": len(rows),
            "trend_ok": n_passed,
            "after_filters": len(kept),
            "shortlist": len([r for r in kept
                              if r["scores"]["global"] >= THR["shortlist_score"]]),
        },
        "errors": errors,
        "rows": sorted(rows, key=lambda r: -r["scores"]["global"]),
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    print(f"\ndata.json: {len(rows)} names, {len(kept)} kept, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
