#!/usr/bin/env python3
"""
Builds universe.csv: PEA-eligible stocks on Euronext Paris, Amsterdam, Brussels,
and Lisbon.

Two strategies, tried in order:
  1. yfinance screener by listing venue (primary source)
  2. universe.seed.csv shipped with the project (fallback, ~110 large caps)

Note: live.euronext.com's own stock table is rendered client-side via
JavaScript after page load, with no documented public JSON endpoint behind it.
An earlier version of this script guessed at an endpoint; it returned invalid
JSON and was dropped rather than kept as dead weight. If Euronext ever
publishes a real data API, worth revisiting.

Output columns: Yahoo ticker, name, ISIN, market, country, sector.
Sectors get filled in later by screener.py (via Yahoo).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
OUT = ROOT / "universe.csv"
SEED = ROOT / "universe.seed.csv"

MIC = {
    "XPAR": ("Paris", ".PA"),
    "XAMS": ("Amsterdam", ".AS"),
    "XBRU": ("Brussels", ".BR"),
    "XLIS": ("Lisbon", ".LS"),
}
EEA = set(CONFIG["universe"]["eligible_countries"])
EXCLUDED = set(CONFIG["universe"]["exclude_tickers"])


# --------------------------------------------------------------------------
# yfinance screener
# --------------------------------------------------------------------------

def from_yfinance(mics: list[str]) -> pd.DataFrame:
    import yfinance as yf
    from yfinance import EquityQuery

    codes = {"XPAR": "PAR", "XAMS": "AMS", "XBRU": "BRU", "XLIS": "LIS"}
    recs = []
    for mic in mics:
        q = EquityQuery("and", [
            EquityQuery("eq", ["exchange", codes[mic]]),
            EquityQuery("gt", ["intradaymarketcap", CONFIG["universe"]["min_market_cap_eur"]]),
        ])
        offset = 0
        while offset < 1000:
            res = yf.screen(q, offset=offset, size=250, sortField="intradaymarketcap", sortAsc=False)
            quotes = res.get("quotes", [])
            if not quotes:
                break
            for x in quotes:
                recs.append({
                    "symbol": x["symbol"].split(".")[0],
                    "name": x.get("longName") or x.get("shortName") or "",
                    "isin": None,
                    "mic": mic,
                })
            offset += len(quotes)
        print(f"  yfinance {mic}: {len([r for r in recs if r['mic']==mic])} names")
    if not recs:
        raise RuntimeError("yfinance screener returned nothing")
    return pd.DataFrame(recs)


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def finalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["market"] = df["mic"].map(lambda m: MIC[m][0])
    df["ticker"] = df.apply(lambda r: f"{r['symbol']}{MIC[r['mic']][1]}", axis=1)
    df["country"] = df["isin"].map(lambda i: i[:2] if isinstance(i, str) else None)
    # Without an ISIN, fall back to the listing venue's country as an approximation
    fallback = {"Paris": "FR", "Amsterdam": "NL", "Brussels": "BE", "Lisbon": "PT"}
    df["country"] = df["country"].fillna(df["market"].map(fallback))
    df["pea_eligible"] = df["country"].isin(EEA)
    df["sector"] = ""

    before = len(df)
    df = df[df["pea_eligible"] & ~df["ticker"].isin(EXCLUDED)]
    df = df.drop_duplicates("ticker").sort_values("ticker")
    print(f"  PEA eligibility: {len(df)} kept out of {before}")
    return df[["ticker", "name", "isin", "market", "country", "sector"]]


def main():
    mics = CONFIG["universe"]["markets"]
    try:
        print("Strategy: yfinance")
        df = finalize(from_yfinance(mics))
        if len(df) >= 150:
            df.to_csv(OUT, index=False)
            print(f"universe.csv written: {len(df)} names")
            return 0
        print(f"  Too few results ({len(df)}), falling back to seed")
    except Exception as e:
        print(f"  Failed: {str(e)[:160]}")

    if SEED.exists():
        print("Falling back to universe.seed.csv")
        pd.read_csv(SEED).to_csv(OUT, index=False)
        return 0
    print("yfinance failed and no fallback file is present.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
