#!/usr/bin/env python3
"""
Daily fetch of US ISO/RTO wholesale market data for the "US ISO daily report".

Outputs under data/<D>/:
  prices.csv      iso,market,location,day,he,interval_start_utc,value
  as_prices.csv   iso,market,product,zone,day,he,interval_start_utc,value
  load.csv        iso,kind,day,he,interval_start_utc,value
  fuel_mix.csv    iso,fuel,day,he,interval_start_utc,value
  gas_prices.csv  source,hub,day,value,unit
  meta.json       dates, time zones, notes, and dataset fetch status
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import traceback
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
import requests

warnings.filterwarnings("ignore")
import gridstatus  # noqa: E402
from gridstatus import Markets  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("fetch_iso")
for _noisy in ("gridstatus", "urllib3", "requests"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

ISO_ORDER = ["CAISO", "ERCOT", "PJM", "MISO", "NYISO", "ISONE", "SPP"]

HUBS = {
    "CAISO": ["TH_SP15_GEN-APND", "TH_NP15_GEN-APND", "TH_ZP26_GEN-APND"],
    "ERCOT": ["HB_NORTH", "HB_HOUSTON", "HB_SOUTH", "HB_WEST", "HB_HUBAVG"],
    "PJM": ["WESTERN HUB", "AEP-DAYTON HUB", "N ILLINOIS HUB", "EASTERN HUB", "DOMINION HUB"],
    "MISO": ["INDIANA.HUB", "ILLINOIS.HUB", "MICHIGAN.HUB", "MINN.HUB", "LOUISIANA.HUB"],
    "NYISO": ["N.Y.C.", "HUD VL", "CAPITL", "WEST", "LONGIL"],
    "ISONE": [".H.INTERNAL_HUB", ".Z.NEMASSBOST", ".Z.CONNECTICUT", ".Z.MAINE"],
    "SPP": ["SPPNORTH_HUB", "SPPSOUTH_HUB"],
}

AS_ID_COLS = {
    "Time", "Interval Start", "Interval End", "Market", "Publish Time",
    "Region", "Zone", "Reserve Zone", "Location", "Location Type", "Locale",
    "Delivery Date", "Delivery Hour", "Delivery Interval", "Hour Ending",
    "DSTFlag", "RepeatedHourFlag", "SCED Timestamp", "Reserve Zone ID",
}

MIX_ID_COLS = {
    "Time", "Interval Start", "Interval End", "Publish Time", "Total", "TOTAL",
}


# --------------------------------------------------------------------------
# context and helpers
# --------------------------------------------------------------------------
@dataclass
class Ctx:
    report_date: pd.Timestamp  # naive calendar date D
    out_root: str = "data"
    prices: list = field(default_factory=list)
    as_prices: list = field(default_factory=list)
    load: list = field(default_factory=list)
    fuel_mix: list = field(default_factory=list)
    gas_prices: list = field(default_factory=list)
    status: dict = field(default_factory=dict)
    notes: dict = field(default_factory=dict)

    def day(self, offset: int) -> str:
        return (self.report_date + pd.Timedelta(days=offset)).strftime("%Y-%m-%d")


def attempt(ctx: Ctx, iso: str, name: str, fn, tries: int = 3, waits=(20, 60)):
    """Run fn() with retries; record status. fn must return a non-empty DataFrame."""
    last = None
    for i in range(tries):
        t0 = time.time()
        try:
            res = fn()
            if res is None or len(res) == 0:
                raise ValueError("empty result after filtering")
            ctx.status.setdefault(iso, {})[name] = {
                "ok": True,
                "rows": int(len(res)),
                "seconds": round(time.time() - t0, 1),
            }
            log.info("%-6s %-22s ok  rows=%-5d %.1fs", iso, name, len(res), time.time() - t0)
            return res
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("%-6s %-22s attempt %d failed: %s: %s", iso, name, i + 1,
                        type(e).__name__, str(e)[:300])
            if os.getenv("FETCH_DEBUG"):
                traceback.print_exc()
            if i < tries - 1:
                time.sleep(waits[min(i, len(waits) - 1)])
    ctx.status.setdefault(iso, {})[name] = {
        "ok": False,
        "error": f"{type(last).__name__}: {str(last)[:400]}",
    }
    log.error("%-6s %-22s FAILED", iso, name)
    return None


def keep(bucket: list, df):
    if df is not None and len(df):
        bucket.append(df)


def to_utc_hour(ts: pd.Series, tz: str) -> pd.Series:
    try:
        s = pd.to_datetime(ts)
        if not pd.api.types.is_datetime64_any_dtype(s):
            s = pd.to_datetime(ts, utc=True)
    except Exception:  # noqa: BLE001
        s = pd.to_datetime(ts, utc=True)
    if getattr(s.dt, "tz", None) is None:
        s = s.dt.tz_localize(tz, ambiguous="infer", nonexistent="shift_forward")
    return s.dt.tz_convert("UTC").dt.floor("h")


def add_day_he(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Add local operating day and hour-ending index (1..23/24/25 on DST days)."""
    local = df["interval_start_utc"].dt.tz_convert(tz)
    df["day"] = local.dt.strftime("%Y-%m-%d")
    midnight_utc = local.dt.normalize().dt.tz_convert("UTC")
    df["he"] = ((df["interval_start_utc"] - midnight_utc) / pd.Timedelta(hours=1)).round().astype(int) + 1
    return df


def pick_col(df: pd.DataFrame, candidates, required=True):
    for c in candidates:
        if c in df.columns:
            return c
    lower = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    if required:
        raise KeyError(f"none of {candidates} in columns {list(df.columns)[:30]}")
    return None


def tidy_prices(df, iso, market, tz, hubs, value_candidates=("LMP", "SPP"),
                loc_candidates=("Location",)):
    value_col = pick_col(df, value_candidates)
    loc_col = None
    for c in loc_candidates:
        if c in df.columns and df[c].astype(str).str.upper().isin([h.upper() for h in hubs]).any():
            loc_col = c
            break
    if loc_col is None:
        sample = {c: df[c].astype(str).unique()[:8].tolist() for c in loc_candidates if c in df.columns}
        raise ValueError(f"hubs {hubs} not found; sample locations {sample}")
    d = df[df[loc_col].astype(str).str.upper().isin([h.upper() for h in hubs])].copy()
    canon = {h.upper(): h for h in hubs}
    d["location"] = d[loc_col].astype(str).str.upper().map(canon)
    d["interval_start_utc"] = to_utc_hour(d["Interval Start"], tz)
    d["value"] = pd.to_numeric(d[value_col], errors="coerce")
    g = d.groupby(["location", "interval_start_utc"], as_index=False)["value"].mean()
    g.insert(0, "iso", iso)
    g.insert(1, "market", market)
    return add_day_he(g, tz)


def tidy_as_wide(df, iso, market, tz, zone_col=None, zone_keep=None, products=None,
                 average_zones=False, zone_label="System"):
    d = df.copy()
    zc = zone_col if (zone_col and zone_col in d.columns) else None
    if zc and zone_keep is not None:
        d = d[d[zc].isin(zone_keep)]
    value_cols = []
    for c in d.columns:
        if c in AS_ID_COLS or c == zc:
            continue
        num = pd.to_numeric(d[c], errors="coerce")
        if num.notna().mean() > 0.5:
            d[c] = num
            value_cols.append(c)
    if products:
        value_cols = [c for c in products if c in value_cols]
    if not value_cols:
        raise ValueError(f"no AS price columns; columns={list(df.columns)}")
    d["interval_start_utc"] = to_utc_hour(d["Interval Start"], tz)
    ids = ["interval_start_utc"] + ([zc] if zc else [])
    long = d.melt(id_vars=ids, value_vars=value_cols, var_name="product", value_name="value")
    if zc and not average_zones:
        long = long.rename(columns={zc: "zone"})
    else:
        long = long.drop(columns=[zc]) if zc else long
        long["zone"] = zone_label
    long["value"] = pd.to_numeric(long["value"], errors="coerce")
    g = long.groupby(["product", "zone", "interval_start_utc"], as_index=False)["value"].mean()
    g.insert(0, "iso", iso)
    g.insert(1, "market", market)
    return add_day_he(g, tz)


def tidy_as_long(df, iso, market, tz, product_col, zone_col, value_col):
    d = df.copy()
    d["interval_start_utc"] = to_utc_hour(d["Interval Start"], tz)
    d["value"] = pd.to_numeric(d[value_col], errors="coerce")
    d = d.rename(columns={product_col: "product", zone_col: "zone"})
    g = d.groupby(["product", "zone", "interval_start_utc"], as_index=False)["value"].mean()
    g.insert(0, "iso", iso)
    g.insert(1, "market", market)
    return add_day_he(g, tz)


def tidy_load(df, iso, kind, tz, value_candidates, day=None, latest_publish=True):
    d = df.copy()
    value_col = pick_col(d, value_candidates)
    if "Interval Start" not in d.columns and "Time" in d.columns:
        d["Interval Start"] = d["Time"]
    pub_col = next((c for c in ("Publish Time", "Evaluated At", "Evaluated At UTC") if c in d.columns), None)
    if latest_publish and pub_col:
        d = d.sort_values(pub_col).groupby("Interval Start", as_index=False).last()
    d["interval_start_utc"] = to_utc_hour(d["Interval Start"], tz)
    d["value"] = pd.to_numeric(d[value_col], errors="coerce")
    g = d.groupby("interval_start_utc", as_index=False)["value"].mean()
    g.insert(0, "iso", iso)
    g.insert(1, "kind", kind)
    g = add_day_he(g, tz)
    if day is not None:
        g = g[g["day"] == day]
    return g


def tidy_fuel_mix(df, iso, tz, day):
    d = df.copy()
    if "Interval Start" not in d.columns and "Time" in d.columns:
        d["Interval Start"] = d["Time"]
    fuel_cols = []
    for c in d.columns:
        if c in MIX_ID_COLS or "flag" in c.lower():
            continue
        num = pd.to_numeric(d[c], errors="coerce")
        if num.notna().mean() > 0.3:
            d[c] = num
            fuel_cols.append(c)
    if not fuel_cols:
        raise ValueError(f"no fuel columns in {list(df.columns)}")
    d["interval_start_utc"] = to_utc_hour(d["Interval Start"], tz)
    long = d.melt(id_vars=["interval_start_utc"], value_vars=fuel_cols, var_name="fuel", value_name="value")
    long["value"] = pd.to_numeric(long["value"], errors="coerce")
    g = long.groupby(["fuel", "interval_start_utc"], as_index=False)["value"].mean()
    g.insert(0, "iso", iso)
    g = add_day_he(g, tz)
    return g[g["day"] == day]


def only_day(df, day):
    return None if df is None else df[df["day"] == day]


# --------------------------------------------------------------------------
# ISO fetchers
# --------------------------------------------------------------------------
def fetch_caiso(ctx: Ctx):
    name, iso = "CAISO", gridstatus.CAISO()
    tz, hubs = iso.default_timezone, HUBS["CAISO"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        nxt = (pd.Timestamp(day) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day, nxt=nxt: only_day(
            tidy_prices(iso.get_lmp(date=day, end=nxt, market=Markets.DAY_AHEAD_HOURLY,
                                    locations=hubs), name, "DA", tz, hubs), day)))

    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", lambda: only_day(
        tidy_prices(iso.get_lmp(date=d2, end=d1, market=Markets.REAL_TIME_5_MIN,
                                locations=hubs), name, "RT", tz, hubs), d2)))

    def as_da():
        df = iso.get_as_prices(date=d1, end=ctx.day(1), market="DAM")
        regions = df["Region"].astype(str).unique().tolist()
        zone = "AS_CAISO_EXP" if "AS_CAISO_EXP" in regions else next(
            (r for r in regions if "CAISO" in r), regions[0])
        ctx.notes["CAISO_as_region"] = zone
        out = tidy_as_wide(df, name, "DA", tz, zone_col="Region", zone_keep=[zone],
                           products=["Regulation Up", "Regulation Down", "Spinning Reserves",
                                     "Non-Spinning Reserves"])
        return out[out["day"].isin([d0, d1])]
    keep(ctx.as_prices, attempt(ctx, name, "as_da_D-1_D", as_da))

    def lf(day, end_day):
        df = iso.get_load_forecast_day_ahead(date=day, end=end_day)
        df = df[df["TAC Area Name"].astype(str).str.upper() == "CA ISO-TAC"]
        return tidy_load(df, name, "forecast", tz, ["Load Forecast"], day=day)

    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: lf(d0, ctx.day(1))))
    keep(ctx.load, attempt(ctx, name, "load_forecast_D-2", lambda: lf(d2, d1)))
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", lambda: tidy_load(
        iso.get_load(date=d2), name, "actual", tz, ["Load"], day=d2)))

    keep(ctx.fuel_mix, attempt(ctx, name, "fuel_mix_D-2", lambda: tidy_fuel_mix(
        iso.get_fuel_mix(date=d2, end=d1), name, tz, d2)))

    def gas_d2():
        df = iso.get_gas_prices(date=d2, end=d1)
        pcol = pick_col(df, ["Price", "Gas Price", "PRC"])
        rcol = pick_col(df, ["Fuel Region Id", "Fuel Region", "Region"], required=False)
        df[pcol] = pd.to_numeric(df[pcol], errors="coerce")
        rows = [{"source": "CAISO", "hub": "CAISO_ALL_MEDIAN", "day": d2,
                 "value": round(float(df[pcol].median()), 3), "unit": "$/MMBtu"}]
        if rcol:
            for reg_prefix, label in (("PGE", "PG&E_Citygate_Proxy"), ("SCE", "SoCal_Proxy")):
                sub = df[df[rcol].astype(str).str.upper().str.startswith(reg_prefix)]
                if len(sub):
                    rows.append({"source": "CAISO", "hub": label, "day": d2,
                                 "value": round(float(sub[pcol].mean()), 3), "unit": "$/MMBtu"})
        return pd.DataFrame(rows)
    keep(ctx.gas_prices, attempt(ctx, name, "gas_D-2", gas_d2, tries=2))


def fetch_ercot(ctx: Ctx):
    name, iso = "ERCOT", gridstatus.Ercot()
    tz, hubs = iso.default_timezone, HUBS["ERCOT"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day: only_day(
            tidy_prices(iso.get_spp(date=day, market=Markets.DAY_AHEAD_HOURLY,
                                    location_type="Trading Hub"), name, "DA", tz, hubs), day)))

    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", lambda: only_day(
        tidy_prices(iso.get_spp(date=d2, market=Markets.REAL_TIME_15_MIN,
                                location_type="Trading Hub"), name, "RT", tz, hubs), d2)))

    for label, day in (("D", d0), ("D-1", d1)):
        keep(ctx.as_prices, attempt(ctx, name, f"as_da_{label}", lambda day=day: only_day(
            tidy_as_wide(iso.get_as_prices(date=day), name, "DA", tz), day)))

    # ERCOT RTC+B real-time ancillary services (15-min MCPC averaged to hourly)
    keep(ctx.as_prices, attempt(ctx, name, "as_rt_D-2", lambda: only_day(
        tidy_as_wide(iso.get_mcpc_real_time_15_min(date=d2), name, "RT", tz), d2), tries=2))

    def lf():
        df = iso.get_load_forecast(date="latest")
        return tidy_load(df, name, "forecast", tz, ["System Total", "SystemTotal", "Total"], day=d0)
    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lf))

    # ERCOT web load forecast is ephemeral; inherit D-2 forecast from data/<D-2>/load.csv if present
    def lf_d2_cached():
        p = os.path.join(ctx.out_root, d2, "load.csv")
        if os.path.exists(p):
            old = pd.read_csv(p, dtype={"day": str})
            sub = old[(old["iso"] == name) & (old["kind"] == "forecast") & (old["day"] == d2)].copy()
            if len(sub):
                sub["interval_start_utc"] = pd.to_datetime(sub["interval_start_utc"], utc=True)
                return sub[["iso", "kind", "day", "he", "interval_start_utc", "value"]]
        return pd.DataFrame()
    cached_ercot_fc = lf_d2_cached()
    if len(cached_ercot_fc):
        keep(ctx.load, cached_ercot_fc)
        ctx.status.setdefault(name, {})["load_forecast_D-2"] = {"ok": True, "rows": int(len(cached_ercot_fc)), "source": "cached_D-2"}

    def la():
        df = iso.get_load_by_weather_zone(date=d2)
        return tidy_load(df, name, "actual", tz, ["System Total", "Total", "Ercot", "ERCOT"],
                         day=d2, latest_publish=False)
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", la))

    keep(ctx.fuel_mix, attempt(ctx, name, "fuel_mix_D-2", lambda: tidy_fuel_mix(
        iso.get_fuel_mix(date=d2), name, tz, d2), tries=2))


def get_pjm_key():
    key = os.getenv("PJM_API_KEY", "").strip()
    if key:
        return key, "secret"
    try:
        r = requests.get("https://dataminer2.pjm.com/config/settings.json", timeout=30)
        j = r.json()

        def find(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, str) and re.search(r"subscription|api.?key", k, re.I) \
                            and re.fullmatch(r"[0-9a-fA-F]{32}", v.strip()):
                        return v.strip()
                    f = find(v)
                    if f:
                        return f
            elif isinstance(o, list):
                for v in o:
                    f = find(v)
                    if f:
                        return f
            return None

        key = find(j)
        if key:
            return key, "public"
    except Exception as e:  # noqa: BLE001
        log.warning("PJM public key lookup failed: %s", e)
    return None, None


def fetch_pjm(ctx: Ctx):
    name = "PJM"
    key, source = get_pjm_key()
    ctx.notes["PJM_key_source"] = source
    if not key:
        ctx.status.setdefault(name, {})["api_key"] = {
            "ok": False, "error": "no PJM API key (set repository secret PJM_API_KEY)"}
        log.error("PJM    no API key available; skipping PJM")
        return
    iso = gridstatus.PJM(api_key=key)
    tz, hubs = iso.default_timezone, HUBS["PJM"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)
    locs = ("Location Name", "Location Short Name", "Location")

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day: only_day(
            tidy_prices(iso.get_lmp(date=day, market=Markets.DAY_AHEAD_HOURLY, locations="hubs"),
                        name, "DA", tz, hubs, loc_candidates=locs), day)))

    def rt():
        try:
            df = iso.get_lmp(date=d2, market=Markets.REAL_TIME_HOURLY, locations="hubs")
            ctx.notes["PJM_rt_source"] = "verified"
        except Exception as e:  # noqa: BLE001
            log.warning("PJM verified RT not available (%s); using unverified", e)
            df = iso.get_lmp_real_time_unverified_hourly(date=d2, location_type="HUB")
            ctx.notes["PJM_rt_source"] = "unverified"
        return only_day(tidy_prices(df, name, "RT", tz, hubs, loc_candidates=locs), d2)
    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", rt))

    for label, day in (("D", d0), ("D-1", d1)):
        keep(ctx.as_prices, attempt(ctx, name, f"as_da_{label}", lambda day=day: only_day(
            tidy_as_long(iso.get_dam_as_market_results(date=day), name, "DA", tz,
                         "Service Type", "Locale", "Market Clearing Price"), day)))

    # Fix: PJM get_load_forecast("today") pivots areas into columns ("PJM RTO")
    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: tidy_load(
        iso.get_load_forecast(date="today"), name, "forecast", tz, ["PJM RTO", "Load Forecast"], day=d0)))
    keep(ctx.load, attempt(ctx, name, "load_forecast_D-2", lambda: tidy_load(
        iso.get_load_forecast_historical(date=d2), name, "forecast", tz, ["PJM RTO", "Load Forecast"], day=d2)))
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", lambda: tidy_load(
        iso.get_load(date=d2), name, "actual", tz, ["Load", "PJM RTO"], day=d2, latest_publish=False)))

    keep(ctx.fuel_mix, attempt(ctx, name, "fuel_mix_D-2", lambda: tidy_fuel_mix(
        iso.get_fuel_mix(date=d2, end=d1), name, tz, d2)))


_RES_PAT = re.compile(r"reg|spin|supp|str|ramp|mcp|reserve", re.I)


def _parse_miso_asm_csv(text: str, day: str, tz: str):
    lines = [l for l in text.splitlines() if l.strip()]
    hdr_idx = None
    for i, l in enumerate(lines):
        cells = [c.strip().strip('"').upper() for c in l.split(",")]
        if sum(bool(re.fullmatch(r"HE ?\d{1,2}", c)) for c in cells) >= 20 or \
                any(c in ("HOUR ENDING", "HOURENDING", "HE") for c in cells):
            hdr_idx = i
            break
    if hdr_idx is None:
        raise ValueError("MISO ASM header not found; head=" + " | ".join(lines[:6])[:400])
    df = pd.read_csv(io.StringIO("\n".join(lines[hdr_idx:])))
    df.columns = [str(c).strip() for c in df.columns]
    he_cols = [c for c in df.columns if re.fullmatch(r"HE ?\d{1,2}", c.upper())]
    base = pd.Timestamp(day).tz_localize(tz)
    if he_cols:
        id_cols = [c for c in df.columns if c not in he_cols]
        nun = {c: df[c].nunique() for c in id_cols}

        def score(c):
            vals = df[c].dropna().astype(str).unique()
            return (sum(bool(_RES_PAT.search(v)) for v in vals) / max(len(vals), 1)) if 1 < len(vals) <= 30 else -1
        prod_col = max(id_cols, key=score) if id_cols and max(score(c) for c in id_cols) > 0 else None
        rest = [c for c in id_cols if c != prod_col]
        zone_col = next((c for c in rest if "ZONE" in c.upper()), None) or \
            (max(rest, key=lambda c: nun[c]) if rest else None)
        long = df.melt(id_vars=id_cols, value_vars=he_cols, var_name="HE", value_name="value")
        long["he_n"] = long["HE"].str.extract(r"(\d+)")[0].astype(int)
        long["product"] = long[prod_col].astype(str) if prod_col else "MCP"
        long["zone"] = long[zone_col].astype(str) if zone_col else "System"
    else:
        he_col = next(c for c in df.columns if c.upper() in ("HOUR ENDING", "HOURENDING", "HE"))
        zone_col = next((c for c in df.columns if "ZONE" in c.upper()), None)
        value_cols = [c for c in df.columns if c not in (he_col, zone_col)]
        long = df.melt(id_vars=[he_col] + ([zone_col] if zone_col else []), value_vars=value_cols,
                       var_name="product", value_name="value")
        long["he_n"] = pd.to_numeric(long[he_col], errors="coerce")
        long["zone"] = long[zone_col].astype(str) if zone_col else "System"
    long = long.dropna(subset=["he_n"])
    long["Interval Start"] = base + pd.to_timedelta(long["he_n"].astype(int) - 1, unit="h")
    long["value"] = pd.to_numeric(long["value"].astype(str).str.replace(",", ""), errors="coerce")
    long = long[["Interval Start", "product", "zone", "value"]].dropna(subset=["value"])
    zone = long["zone"].astype(str).str.strip()
    wide = long[zone.str.upper().str.contains("MISO WIDE")]
    if len(wide):
        long = wide.assign(zone="MISO Wide")
    elif long["zone"].nunique() > 20:
        long = long.groupby(["Interval Start", "product"], as_index=False)["value"].median()
        long["zone"] = "全网节点中位数"
    prod = long["product"].astype(str).str.upper().str.strip()
    if prod.str.startswith("GEN").any():
        long = long[prod.str.startswith("GEN")]
    long["product"] = long["product"].map(_miso_product_name)
    return long


def _miso_product_name(code: str) -> str:
    base = re.sub(r"^(GEN|DEM|SER)", "", str(code).upper().strip())
    base = re.sub(r"MCP$", "", base)
    return {"REG": "Regulation", "SPIN": "Spinning Reserve", "SUPP": "Supplemental Reserve",
            "STR": "Short-Term Reserve", "RAMPUP": "Ramp Up", "RCUP": "Ramp Up", "URC": "Ramp Up",
            "RAMPDN": "Ramp Down", "RAMPDOWN": "Ramp Down", "RCDN": "Ramp Down", "DRC": "Ramp Down"}.get(base, str(code))


def fetch_miso(ctx: Ctx):
    name, iso = "MISO", gridstatus.MISO()
    tz, hubs = iso.default_timezone, HUBS["MISO"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day: only_day(
            tidy_prices(iso.get_lmp_day_ahead_hourly(date=day), name, "DA", tz, hubs), day)))

    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", lambda: only_day(
        tidy_prices(iso.get_lmp_real_time_hourly_prelim(date=d2), name, "RT", tz, hubs), d2)))

    def as_day(day):
        key = os.getenv("MISO_API_PRICING_SUBSCRIPTION_KEY", "").strip()
        if key:
            from gridstatus.miso_api import MISOAPI
            df = MISOAPI(pricing_api_key=key).get_as_mcp_day_ahead_ex_ante(date=day)
            prod = pick_col(df, ["Product", "Reserve Product", "Ancillary Service"])
            zone = pick_col(df, ["Zone", "Reserve Zone"])
            val = pick_col(df, ["MCP", "Market Clearing Price", "Price"])
            out = tidy_as_long(df, name, "DA", tz, prod, zone, val)
        else:
            url = f"https://docs.misoenergy.org/marketreports/{day.replace('-', '')}_asm_exante_damcp.csv"
            r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            raw = _parse_miso_asm_csv(r.text, day, tz)
            out = tidy_as_long(raw, name, "DA", tz, "product", "zone", "value")
        return only_day(out, day)

    for label, day in (("D", d0), ("D-1", d1)):
        keep(ctx.as_prices, attempt(ctx, name, f"as_da_{label}", lambda day=day: as_day(day)))

    def dfal(day):
        return iso._get_load_forecast_file(pd.Timestamp(day).tz_localize(tz))

    def lf_target(target_day, search_days):
        for day in search_days:
            try:
                df = dfal(day)
            except Exception as e:  # noqa: BLE001
                log.warning("MISO df_al %s not available: %s", day, e)
                continue
            col = next((c for c in df.columns if "MISO" in c.upper() and "MTLF" in c.upper()), None)
            if col is None:
                continue
            out = tidy_load(df.dropna(subset=[col]), name, "forecast", tz, [col], day=target_day,
                            latest_publish=False)
            if len(out):
                return out
        return None
    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: lf_target(d0, (d0, d1))))
    keep(ctx.load, attempt(ctx, name, "load_forecast_D-2", lambda: lf_target(d2, (d2, d1))))

    def la():
        for day in (d0, d1, d2):
            try:
                df = dfal(day)
            except Exception as e:  # noqa: BLE001
                log.warning("MISO df_al %s not available: %s", day, e)
                continue
            col = next((c for c in df.columns if "MISO" in c.upper() and "ACTUAL" in c.upper()), None)
            if col is None:
                continue
            out = tidy_load(df.dropna(subset=[col]), name, "actual", tz, [col], day=d2,
                            latest_publish=False)
            if len(out) >= 20:
                return out
        return None
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", la))

    keep(ctx.fuel_mix, attempt(ctx, name, "fuel_mix_D-2", lambda: tidy_fuel_mix(
        iso.get_fuel_mix(date=d2), name, tz, d2), tries=2))


def fetch_nyiso(ctx: Ctx):
    name, iso = "NYISO", gridstatus.NYISO()
    tz, hubs = iso.default_timezone, HUBS["NYISO"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day: only_day(
            tidy_prices(iso.get_lmp_day_ahead_hourly(date=day), name, "DA", tz, hubs), day)))

    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", lambda: only_day(
        tidy_prices(iso.get_lmp_real_time_hourly(date=d2), name, "RT", tz, hubs), d2)))

    def as_day(day):
        df = iso.get_as_prices_day_ahead_hourly(date=day)
        zone_col = pick_col(df, ["Zone", "Region", "Location"], required=False)
        return only_day(tidy_as_wide(df, name, "DA", tz, zone_col=zone_col), day)
    for label, day in (("D", d0), ("D-1", d1)):
        keep(ctx.as_prices, attempt(ctx, name, f"as_da_{label}", lambda day=day: as_day(day)))

    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: tidy_load(
        iso.get_load_forecast(date=d0), name, "forecast", tz, ["Load Forecast", "NYISO"], day=d0)))
    keep(ctx.load, attempt(ctx, name, "load_forecast_D-2", lambda: tidy_load(
        iso.get_load_forecast(date=d2), name, "forecast", tz, ["Load Forecast", "NYISO"], day=d2)))
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", lambda: tidy_load(
        iso.get_load(date=d2), name, "actual", tz, ["Load"], day=d2, latest_publish=False)))

    keep(ctx.fuel_mix, attempt(ctx, name, "fuel_mix_D-2", lambda: tidy_fuel_mix(
        iso.get_fuel_mix(date=d2), name, tz, d2)))


def fetch_isone(ctx: Ctx):
    name, iso = "ISONE", gridstatus.ISONE()
    tz, hubs = iso.default_timezone, HUBS["ISONE"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day: only_day(
            tidy_prices(iso.get_lmp_day_ahead_hourly(date=day), name, "DA", tz, hubs), day)))

    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", lambda: only_day(
        tidy_prices(iso.get_lmp_real_time_hourly(date=d2), name, "RT", tz, hubs), d2)))

    def reserve_prices_fallback(kind):
        from gridstatus.isone import _make_request
        ds = pd.Timestamp(d2).strftime("%Y%m%d")
        url = f"https://www.iso-ne.com/transform/csv/fiveminreserveprice?type={kind}&start={ds}&end={ds}"
        df = _make_request(url, skiprows=[0, 1, 2, 3, 5], verbose=False)
        df.columns = df.columns.str.strip().str.replace('"', "")
        if df.empty or "Local Time" not in df.columns:
            raise ValueError(f"ISO-NE {kind} reserve prices empty for {d2}")
        df["Interval Start"] = df.groupby("Reserve Zone ID", group_keys=False)["Local Time"].apply(
            lambda x: pd.to_datetime(x).dt.tz_localize(tz, ambiguous="infer"))
        return df.rename(columns={
            "Ten-Minute Spinning Reserve Clearing Price": "TMSR Clearing Price",
            "Ten-Minute Reserve Clearing Price": "TMR Clearing Price"})

    def as_rt():
        df, errs = None, []
        try:
            df = iso.get_reserve_zone_prices_designations_real_time_5_min_final(date=d2)
            if len(df):
                ctx.notes["ISONE_reserve_source"] = "final_public_api"
        except Exception as e:  # noqa: BLE001
            errs.append(f"public_final: {e}")
            df = None
        if df is None or df.empty:
            for kind in ("final", "prelim", "preliminary"):
                try:
                    df = reserve_prices_fallback(kind)
                    if len(df):
                        ctx.notes["ISONE_reserve_source"] = kind
                        break
                except Exception as e:  # noqa: BLE001
                    errs.append(f"{kind}: {e}")
                    df = None
        if df is None or df.empty:
            raise ValueError("; ".join(errs)[:400])
        zone_col = next((c for c in df.columns if "zone" in c.lower() and "name" in c.lower()), None) or \
            next((c for c in df.columns if "zone" in c.lower() and "id" not in c.lower()
                  and not pd.api.types.is_numeric_dtype(df[c])), None)
        price_cols = [c for c in df.columns if "price" in c.lower()]
        if not price_cols:
            raise ValueError(f"no price columns in {list(df.columns)}")
        keep_cols = ["Interval Start"] + ([zone_col] if zone_col else []) + price_cols
        return only_day(tidy_as_wide(df[keep_cols], name, "RT", tz, zone_col=zone_col), d2)
    keep(ctx.as_prices, attempt(ctx, name, "as_rt_D-2", as_rt))

    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: tidy_load(
        iso.get_load_forecast(date=d0), name, "forecast", tz, ["Load Forecast"], day=d0)))
    keep(ctx.load, attempt(ctx, name, "load_forecast_D-2", lambda: tidy_load(
        iso.get_load_forecast(date=d2), name, "forecast", tz, ["Load Forecast"], day=d2)))
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", lambda: tidy_load(
        iso.get_load(date=d2), name, "actual", tz, ["Load"], day=d2, latest_publish=False)))

    keep(ctx.fuel_mix, attempt(ctx, name, "fuel_mix_D-2", lambda: tidy_fuel_mix(
        iso.get_fuel_mix(date=d2), name, tz, d2)))


def fetch_spp(ctx: Ctx):
    name, iso = "SPP", gridstatus.SPP()
    tz, hubs = iso.default_timezone, HUBS["SPP"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day: only_day(
            tidy_prices(iso.get_lmp_day_ahead_hourly(date=day, location_type="Hub"),
                        name, "DA", tz, hubs), day)))

    def rt():
        try:
            df = iso.get_lmp_real_time_5_min_by_location(date=d2, location_type="Hub", use_daily_files=True)
            ctx.notes["SPP_rt_source"] = "daily file"
        except Exception as e:  # noqa: BLE001
            log.warning("SPP daily RT file not available (%s); using 5-minute interval files", e)
            start = pd.Timestamp(d2).tz_localize(tz)
            df = iso.get_lmp_real_time_5_min_by_location(date=start, end=start + pd.Timedelta(days=1),
                                                         location_type="Hub")
            ctx.notes["SPP_rt_source"] = "5-minute interval files"
        return only_day(tidy_prices(df, name, "RT", tz, hubs), d2)
    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", rt, tries=2))

    for label, day in (("D", d0), ("D-1", d1)):
        keep(ctx.as_prices, attempt(ctx, name, f"as_da_{label}", lambda day=day: only_day(
            tidy_as_wide(iso.get_day_ahead_operating_reserve_prices(date=day), name, "DA", tz,
                         zone_col="Reserve Zone", average_zones=True, zone_label="Zone avg"), day)))

    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: tidy_load(
        iso.get_load_forecast(date=d0), name, "forecast", tz, ["Load Forecast"], day=d0)))
    keep(ctx.load, attempt(ctx, name, "load_forecast_D-2", lambda: tidy_load(
        iso.get_load_forecast(date=d2), name, "forecast", tz, ["Load Forecast"], day=d2)))

    def la():
        errors = []
        try:
            t = pd.Timestamp(d1).tz_localize(tz) + pd.Timedelta(minutes=20)
            df = iso.get_load_forecast_mid_term(date=t)
            col = next((c for c in df.columns if "actual" in c.lower()), None)
            if col is not None:
                df[col] = pd.to_numeric(df[col], errors="coerce")
                g = df.dropna(subset=[col]).groupby("Interval Start", as_index=False)[col].sum(min_count=1)
                out = tidy_load(g, name, "actual", tz, [col], day=d2, latest_publish=False)
                if len(out) >= 20:
                    ctx.notes["SPP_load_actual_source"] = "MTLF averaged actual"
                    return out
                errors.append(f"MTLF actual rows for {d2}: {len(out)}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"MTLF: {e}")
        try:
            df = iso.get_hourly_load(date=d2)
            ba = df[df["Forecast Area Type"].astype(str).str.upper().str.contains("BA")]
            use = ba if len(ba) else df
            g = use.groupby("Interval Start", as_index=False)["Load"].sum(min_count=1)
            out = tidy_load(g, name, "actual", tz, ["Load"], day=d2, latest_publish=False)
            if len(out) >= 20:
                ctx.notes["SPP_load_actual_source"] = "daily hourly load file"
                return out
            errors.append(f"hourly load rows: {len(out)}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"hourly load: {e}")
        raise ValueError("; ".join(errors)[:400])
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", la, tries=2))

    keep(ctx.fuel_mix, attempt(ctx, name, "fuel_mix_D-2", lambda: tidy_fuel_mix(
        iso.get_fuel_mix(date=d2), name, tz, d2)))


def fetch_eia_gas(ctx: Ctx):
    """Optional Henry Hub spot gas price if EIA_API_KEY env secret is configured."""
    key = os.getenv("EIA_API_KEY", "").strip()
    if not key:
        return
    d2 = ctx.day(-2)
    start = (pd.Timestamp(d2) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")

    def pull():
        url = "https://api.eia.gov/v2/natural-gas/pri/fut/data/"
        params = {
            "api_key": key,
            "frequency": "daily",
            "data[0]": "value",
            "facets[series][]": "RNGWHHD",
            "start": start,
            "end": d2,
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": 10,
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json().get("response", {}).get("data", [])
        rows = []
        for row in data:
            val = pd.to_numeric(row.get("value"), errors="coerce")
            if pd.notna(val):
                rows.append({"source": "EIA", "hub": "Henry_Hub_Spot",
                             "day": str(row["period"])[:10], "value": round(float(val), 3),
                             "unit": "$/MMBtu"})
        return pd.DataFrame(rows[:5])
    keep(ctx.gas_prices, attempt(ctx, "EIA", "henry_hub_gas", pull, tries=2))


FETCHERS = {
    "CAISO": fetch_caiso, "ERCOT": fetch_ercot, "PJM": fetch_pjm, "MISO": fetch_miso,
    "NYISO": fetch_nyiso, "ISONE": fetch_isone, "SPP": fetch_spp,
}


# --------------------------------------------------------------------------
def default_report_date() -> pd.Timestamp:
    now_bj = datetime.now(timezone.utc) + pd.Timedelta(hours=8)
    return pd.Timestamp(now_bj.date())


def _merge_csv(path: str, new: pd.DataFrame, keys: list[str], cols: list[str]) -> pd.DataFrame:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            old = pd.read_csv(path, dtype={"day": str})
        except Exception:  # noqa: BLE001
            old = pd.DataFrame(columns=cols)
        if len(old) and len(new):
            new_keys = set(map(tuple, new[keys].astype(str).values))
            mask = [tuple(r) not in new_keys for r in old[keys].astype(str).values]
            old = old[mask]
        frames = [f for f in (old, new) if len(f)]
        return pd.concat(frames, ignore_index=True)[cols] if frames else pd.DataFrame(columns=cols)
    return new[cols] if len(new) else pd.DataFrame(columns=cols)


def write_outputs(ctx: Ctx, out_dir: str, isos):
    os.makedirs(out_dir, exist_ok=True)

    def dump(frames, name, cols, keys, round_nd=3, has_utc=True):
        path = os.path.join(out_dir, name)
        if frames:
            df = pd.concat(frames, ignore_index=True)
            if has_utc and "interval_start_utc" in df.columns:
                df["interval_start_utc"] = pd.to_datetime(df["interval_start_utc"], utc=True).dt.strftime("%Y-%m-%dT%H:%MZ")
            df["value"] = pd.to_numeric(df["value"], errors="coerce").round(round_nd)
            df = df[cols]
        else:
            df = pd.DataFrame(columns=cols)
        df = _merge_csv(path, df, keys, cols)
        sort_cols = [c for c in cols if c != "value"]
        df = df.sort_values(sort_cols).reset_index(drop=True)
        df.to_csv(path, index=False)
        return len(df)

    n_p = dump(ctx.prices, "prices.csv",
               ["iso", "market", "location", "day", "he", "interval_start_utc", "value"],
               ["iso", "market", "day"])
    n_a = dump(ctx.as_prices, "as_prices.csv",
               ["iso", "market", "product", "zone", "day", "he", "interval_start_utc", "value"],
               ["iso", "market", "day"])
    n_l = dump(ctx.load, "load.csv",
               ["iso", "kind", "day", "he", "interval_start_utc", "value"],
               ["iso", "kind", "day"])
    n_f = dump(ctx.fuel_mix, "fuel_mix.csv",
               ["iso", "fuel", "day", "he", "interval_start_utc", "value"],
               ["iso", "day"])
    n_g = dump(ctx.gas_prices, "gas_prices.csv",
               ["source", "hub", "day", "value", "unit"],
               ["source", "hub", "day"], has_utc=False)

    meta_path = os.path.join(out_dir, "meta.json")
    old_meta = {}
    if os.path.exists(meta_path):
        try:
            old_meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old_meta = {}
    status = old_meta.get("status", {})
    for iso, sets in ctx.status.items():
        for name, st in sets.items():
            prev = status.get(iso, {}).get(name)
            if st.get("ok") or not (prev and prev.get("ok")):
                status.setdefault(iso, {})[name] = st
            else:
                prev["kept_from_earlier_run"] = True
    notes = {**old_meta.get("notes", {}), **ctx.notes}
    runs = old_meta.get("runs", []) + [{
        "at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "isos": list(isos)}]
    tzs = {"CAISO": "US/Pacific", "ERCOT": "US/Central", "PJM": "US/Eastern", "MISO": "EST",
           "NYISO": "US/Eastern", "ISONE": "US/Eastern", "SPP": "US/Central"}
    meta = {
        "report_date": ctx.day(0),
        "da_day": ctx.day(0),
        "da_prev_day": ctx.day(-1),
        "rt_day": ctx.day(-2),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gridstatus_version": gridstatus.__version__,
        "timezones": tzs,
        "hubs": HUBS,
        "rows": {"prices": n_p, "as_prices": n_a, "load": n_l, "fuel_mix": n_f, "gas_prices": n_g},
        "status": status,
        "notes": notes,
        "runs": runs,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta


def failed_isos(out_dir: str) -> list[str]:
    meta_path = os.path.join(out_dir, "meta.json")
    if not os.path.exists(meta_path):
        return list(ISO_ORDER)
    status = json.load(open(meta_path, encoding="utf-8")).get("status", {})
    return [iso for iso in ISO_ORDER
            if iso not in status or any(not v.get("ok") for v in status[iso].values())]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="report date D (YYYY-MM-DD, Beijing). Default: today in Beijing")
    ap.add_argument("--out", default="data", help="output root directory")
    ap.add_argument("--isos", default=",".join(ISO_ORDER), help="comma-separated subset")
    ap.add_argument("--retry-failed", action="store_true",
                    help="only re-fetch ISOs that have a failed dataset in today's meta.json")
    args = ap.parse_args()

    report_date = pd.Timestamp(args.date) if args.date else default_report_date()
    ctx = Ctx(report_date=report_date, out_root=args.out)
    out_dir = os.path.join(args.out, ctx.day(0))
    os.makedirs(out_dir, exist_ok=True)
    fh = logging.FileHandler(os.path.join(out_dir, "fetch.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

    isos = [s_.strip().upper() for s_ in args.isos.split(",") if s_.strip()]
    if args.retry_failed:
        isos = [i for i in isos if i in failed_isos(out_dir)]
        if not isos:
            log.info("retry-failed: nothing to retry for %s", ctx.day(0))
            return
    log.info("report date D=%s | DA days %s,%s,%s | RT day %s | ISOs %s", ctx.day(0), ctx.day(-2),
             ctx.day(-1), ctx.day(0), ctx.day(-2), ",".join(isos))

    for iso in isos:
        t0 = time.time()
        try:
            FETCHERS[iso](ctx)
        except Exception as e:  # noqa: BLE001
            ctx.status.setdefault(iso, {})["init"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            log.error("%s fetcher crashed: %s", iso, e)
            traceback.print_exc()
        log.info("%s done in %.0fs", iso, time.time() - t0)

    fetch_eia_gas(ctx)
    meta = write_outputs(ctx, out_dir, isos)
    ok = sum(v.get("ok", False) for s_ in ctx.status.values() for v in s_.values())
    total = sum(len(s_) for s_ in ctx.status.values())
    log.info("this run: datasets ok %d/%d | file rows %s", ok, total, meta["rows"])
    for iso, st in meta["status"].items():
        bad = [k for k, v in st.items() if not v.get("ok")]
        if bad:
            log.info("  %s still failing: %s", iso, ", ".join(bad))
    sys.exit(0 if ok > 0 else 1)


if __name__ == "__main__":
    main()