"""Price data tool, backed by yfinance.

Prices are split-adjusted (auto_adjust=True), which is correct for
returns but hides splits from the price series itself, so any splits in
the window are reported explicitly. A ticker that cannot be priced is a
normal, expected outcome in this dataset: unresolved CUSIPs have no
ticker, and some resolved tickers are foreign-listing symbols Yahoo does
not know. Those cases return ok=False with a suggestion, not an error.
"""

from __future__ import annotations

import yfinance as yf


def get_price_summary(ticker: str | None, start: str, end: str) -> dict:
    """Price move summary for a ticker between two ISO dates."""
    try:
        if not ticker:
            return {"ok": False,
                    "error": "no ticker is available for this security, likely an "
                             "unresolved CUSIP; prices cannot be fetched. Consider "
                             "searching news using the company name instead."}
        t = yf.Ticker(ticker)
        hist = t.history(start=start, end=end, auto_adjust=True)
        if hist.empty:
            return {"ok": False,
                    "error": f"no price data found for '{ticker}' on Yahoo Finance; "
                             "it may be delisted, renamed, or a foreign-listing "
                             "symbol. Consider searching news using the company "
                             "name instead."}
        closes = hist["Close"]
        start_price = float(closes.iloc[0])
        end_price = float(closes.iloc[-1])
        splits = {}
        try:
            for d, ratio in t.splits.items():
                day = str(d.date())
                if start <= day <= end:
                    splits[day] = float(ratio)
        except Exception:
            pass  # split data is best-effort; price summary stands alone
        return {
            "ok": True,
            "ticker": ticker,
            "start_date": str(hist.index[0].date()),
            "end_date": str(hist.index[-1].date()),
            "start_price": round(start_price, 2),
            "end_price": round(end_price, 2),
            "pct_change": round((end_price / start_price - 1) * 100, 2),
            "high": round(float(closes.max()), 2),
            "low": round(float(closes.min()), 2),
            "splits_in_window": splits,
            "note": "prices are split-adjusted; splits_in_window lists any stock "
                    "splits in the period as ratio of new shares per old share",
        }
    except Exception as e:
        return {"ok": False, "error": f"price lookup for '{ticker}' failed: {e}"}
