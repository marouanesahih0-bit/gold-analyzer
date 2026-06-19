#!/usr/bin/env python3
"""
update_data.py — يجلب أحدث الأرقام الاقتصادية من FRED API
ويكتبها في data.json بصيغة جاهزة لصفحة محلل الذهب.

يعمل من جهة السيرفر (GitHub Actions) — لا توجد مشاكل CORS هنا
لأن السيرفر يستدعي السيرفر مباشرة، وليس متصفح.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

if not FRED_API_KEY:
    print("ERROR: FRED_API_KEY environment variable is not set.", file=sys.stderr)
    sys.exit(1)


def fred_series(series_id, limit=14):
    """Fetch last `limit` observations for a FRED series, newest first."""
    url = (
        f"{FRED_BASE}?series_id={series_id}&api_key={FRED_API_KEY}"
        f"&file_type=json&sort_order=desc&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "gold-analyzer-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "error_message" in data:
        raise RuntimeError(f"FRED error for {series_id}: {data['error_message']}")

    obs = [
        {"date": o["date"], "value": float(o["value"])}
        for o in data.get("observations", [])
        if o["value"] != "."
    ]
    return obs


def yoy(series_id):
    """Year-over-year % change (latest vs ~12 months ago)."""
    obs = fred_series(series_id, limit=14)
    if len(obs) < 13:
        raise RuntimeError(f"Not enough data for YoY calc on {series_id}")
    latest, year_ago = obs[0]["value"], obs[12]["value"]
    return round((latest - year_ago) / year_ago * 100, 2)


def mom(series_id):
    """Month-over-month % change."""
    obs = fred_series(series_id, limit=2)
    if len(obs) < 2:
        raise RuntimeError(f"Not enough data for MoM calc on {series_id}")
    latest, prev = obs[0]["value"], obs[1]["value"]
    return round((latest - prev) / prev * 100, 2)


def fetch_gold_price():
    """Fetch live gold price — no API key needed, public endpoint."""
    try:
        req = urllib.request.Request(
            "https://api.gold-api.com/price/XAU",
            headers={"User-Agent": "gold-analyzer-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return round(float(data["price"]), 2)
    except Exception as e:
        print(f"WARNING: could not fetch gold price: {e}", file=sys.stderr)
        return None


def safe(fn, *args, label=""):
    """Run fn, return (value, error_or_None). Never raises."""
    try:
        return fn(*args), None
    except Exception as e:
        print(f"WARNING: failed to fetch {label}: {e}", file=sys.stderr)
        return None, str(e)


def main():
    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "values": {},
        "errors": {},
    }

    def set_field(key, fn, *args):
        val, err = safe(fn, *args, label=key)
        if val is not None:
            result["values"][key] = val
        if err:
            result["errors"][key] = err

    # --- Fed Funds Rate ---
    def fed_rate():
        obs = fred_series("FEDFUNDS", limit=1)
        return round(obs[0]["value"], 2)
    set_field("fed_rate", fed_rate)

    # --- Core PCE (annual + monthly) ---
    set_field("core_pce_annual", yoy, "PCEPILFE")
    set_field("core_pce_monthly", mom, "PCEPILFE")

    # --- Headline PCE monthly ---
    set_field("pce_monthly", mom, "PCEPI")

    # --- CPI + Core CPI (YoY) ---
    set_field("cpi_yoy", yoy, "CPIAUCSL")
    set_field("core_cpi_yoy", yoy, "CPILFESL")

    # --- NFP (level change) + Unemployment ---
    def nfp_pair():
        obs = fred_series("PAYEMS", limit=3)
        actual = round(obs[0]["value"] - obs[1]["value"])
        previous = round(obs[1]["value"] - obs[2]["value"])
        return {"actual": actual, "previous": previous}
    nfp_val, nfp_err = safe(nfp_pair, label="nfp")
    if nfp_val:
        result["values"]["nfp_actual"] = nfp_val["actual"]
        result["values"]["nfp_previous"] = nfp_val["previous"]
    if nfp_err:
        result["errors"]["nfp"] = nfp_err

    def unemployment():
        obs = fred_series("UNRATE", limit=1)
        return round(obs[0]["value"], 1)
    set_field("unemployment_rate", unemployment)

    # --- GDP (real GDP growth rate, quarterly) ---
    def gdp_pair():
        obs = fred_series("A191RL1Q225SBEA", limit=2)
        return {"actual": round(obs[0]["value"], 1), "previous": round(obs[1]["value"], 1)}
    gdp_val, gdp_err = safe(gdp_pair, label="gdp")
    if gdp_val:
        result["values"]["gdp_actual"] = gdp_val["actual"]
        result["values"]["gdp_previous"] = gdp_val["previous"]
    if gdp_err:
        result["errors"]["gdp"] = gdp_err

    # --- Treasury Yields ---
    def yield_10y():
        obs = fred_series("DGS10", limit=5)
        return round(obs[0]["value"], 2)
    set_field("yield_10y", yield_10y)

    def yield_2y():
        obs = fred_series("DGS2", limit=5)
        return round(obs[0]["value"], 2)
    set_field("yield_2y", yield_2y)

    # --- DXY (trade-weighted dollar index) ---
    def dxy():
        obs = fred_series("DTWEXBGS", limit=5)
        return round(obs[0]["value"], 2)
    set_field("dxy", dxy)

    # --- Oil WTI ---
    def oil():
        obs = fred_series("DCOILWTICO", limit=5)
        return round(obs[0]["value"], 1)
    set_field("oil_wti", oil)

    # --- Gold price (live, no key) ---
    gold_price = fetch_gold_price()
    if gold_price is not None:
        result["values"]["gold_price"] = gold_price

    # write output
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data.json")
    out_path = os.path.normpath(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Wrote {out_path}")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result["errors"]:
        print(f"\n{len(result['errors'])} field(s) failed — see warnings above.", file=sys.stderr)


if __name__ == "__main__":
    main()
