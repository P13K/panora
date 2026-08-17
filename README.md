# Panora — PEA screener for Euronext

A trend-following + quality screener over **every** stock eligible for the French
PEA tax wrapper, listed on Euronext Paris, Amsterdam, Brussels, and Lisbon. The
name: pan the whole river (all of Euronext), filter like a prospector (trend
first, fundamentals next), keep the nuggets (the shortlist). Data refreshes
automatically, the page is a static HTML file served on GitHub Pages. Cost: $0.

This is a **first-pass** tool: it sorts and points research in the right
direction, it does not make the call for you. Always confirm PEA eligibility
with your broker before placing an order — the country filter here is a
reliable approximation, not a legal guarantee.

## How it works

```
build_universe.py ──► universe.csv ──► screener.py ──► data.json ──► index.html
 (Euronext listing,    (~500-600       (2-stage,         (static)      (public page)
  PEA filter)            names)          config-driven)
```

No API key, no server. The page just does a `fetch("data.json")` — all the
computation happens ahead of time in the scheduled action.

## Getting started (15 minutes)

1. Create a **public** GitHub repo (Pages is free only on public repos), push
   these files.
2. `Settings ▸ Pages ▸ Source` → **GitHub Actions**.
3. `Actions` tab → *Refresh screener* workflow → **Run workflow**.
4. The public URL shows up once the job finishes.

Running locally:

```bash
pip install -r requirements.txt
python build_universe.py    # writes universe.csv
python screener.py          # writes data.json (10-20 min on the full universe)
python -m http.server 8000  # http://localhost:8000
```

Without a `data.json`, the page falls back to embedded demo data (made-up
numbers, orange banner) so you can judge the interface before the first run.

## The universe

`build_universe.py` uses the yfinance screener, queried separately for each
of the four listing venues, and falls back to `universe.seed.csv` (the ~110
large caps shipped with the project) if that returns too little.

In practice this lands around 500-600 PEA-eligible names — smaller than a
full Euronext instrument count (which runs past 1,000 once every micro-cap
and illiquid Growth/Access listing is included), but those extra names
typically lack the price history needed to score them anyway, so the
practical universe is close to this either way.

Euronext's own live.euronext.com stock table is rendered client-side by
JavaScript with no documented public JSON endpoint behind it, so it isn't
used as a data source here. If that ever changes, this is the natural place
to add a richer strategy back in.

The result is archived as a build artifact on every run, so you can check
how many names came back.

**PEA eligibility** is filtered on the issuer's country, inferred from the
first two letters of the ISIN when available, or from the listing venue as
an approximation otherwise, checked against `universe.eligible_countries` in
`config.json`. This is a good first pass — exotic holding structures
sometimes slip through. Always confirm with your broker before the order.

## Settings — `config.json`

Everything lives here, nothing is hardcoded. Active settings also show at the
top of the page under "Settings applied to this run".

| Block | What you tune |
|---|---|
| `universe` | Markets, eligible countries, minimum market cap, minimum liquidity, banned tickers |
| `elimination_filters` | The 6 hard filters. `enabled: false` turns them all off and shows the raw universe |
| `score_weights` | 40 / 25 / 20 / 15 by default |
| `trend_grid` / `quality_grid` / `growth_grid` / `value_grid` | The thresholds behind each 1-to-5 score |
| `half_price_5y_valuation` | Target CAGR (15%), growth cap, exit P/E cap |
| `display_thresholds` | Quality thresholds for the green/red pills in the detail panel — informational only, don't exclude anything |
| `execution` | Benchmark index, call pacing, two-stage filtering toggle |

## Two stages, and why

Downloading fundamentals for 1,200 names one by one takes hours and trips
Yahoo's rate limits. So:

- **Stage 1** — prices for the whole universe in a handful of batched calls.
  Trend, liquidity, MA200. Two-thirds of the universe typically drops out
  here, which is expected: the MA200 is a hard filter.
- **Stage 2** — fundamentals, only for survivors. A name excluded at stage 1
  still appears in the table, with its reason, just without ratios.

To force fundamentals for everything despite the extra time:
`execution.fundamentals_only_if_trend_passes: false`.

## On small caps

This is where the inefficiencies live, and where the data is most fragile.
Two guardrails in `config.json`:

- `min_market_cap_eur` (€50M by default) — below that, free float is often
  too thin for a position of a few thousand euros.
- `min_daily_turnover_eur` (€100k by default) — a stock trading €30k/day will
  cost you more in spread than in broker fees, and will be painful to exit.
  This is an execution-cost filter, not a judgment on the company.

Lower both to cast a wider net, knowingly.

## Limitations

- **Fundamentals via Yahoo Finance** (`yfinance`, an unofficial API): free,
  good enough for a first pass, but ratios can lag a quarter and are often
  missing for micro caps. Names without usable data are listed under
  `data.json ▸ errors`.
- The screener ranks, it doesn't decide. The "Copy research brief" button
  exports the shortlist in a format ready for hands-on fundamental analysis.

## Moving to a paid data source

Only `compute_fundamentals()` needs rewriting — the rest of the pipeline
doesn't change. For Euronext coverage: EODHD (60+ exchanges, long history) or
Financial Modeling Prep (dedicated Euronext endpoint). Expect a few tens of
euros per month for non-US fundamentals.
