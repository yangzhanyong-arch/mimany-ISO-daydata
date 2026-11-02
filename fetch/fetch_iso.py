#!/usr/bin/env python3
"""
Daily fetch of US ISO/RTO wholesale market data for the "US ISO daily report".

Runs on GitHub Actions (needs open internet access to the ISO websites).

For a report date D (the Beijing calendar date the report is read on):
  * Day-ahead (DA) LMP at key hubs for US operating days D-2, D-1 and D
  * Real-time (RT) LMP at key hubs for operating day D-2 (the latest complete
    US operating day when the report is produced), averaged to hourly
  * Ancillary-service (AS) clearing prices: day-ahead for D-1 and D
    (ISO-NE has no public day-ahead AS feed in gridstatus, so its real-time
    reserve prices for D-2 are used instead)
  * Load: latest load forecast for D, actual load for D-2 (hourly)

Output: tidy CSVs with UTC timestamps plus local operating day and hour-ending:
  data/<D>/prices.csv     iso,market,location,day,he,interval_start_utc,value
  data/<D>/as_prices.csv  iso,market,product,zone,day,he,interval_start_utc,value
  data/<D>/load.csv       iso,kind,day,he,interval_start_utc,value
  data/<D>/meta.json      dates, time zones and the status of every dataset
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

# Pricing locations shown in the report (first one is the headline hub).
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
}


# --------------------------------------------------------------------------
# context and helpers
# --------------------------------------------------------------------------
@dataclass
class Ctx:
    report_date: pd.Timestamp  # naive calendar date D
    prices: list = field(default_factory=list)
    as_prices: list = field(default_factory=list)
    load: list = field(default_factory=list)
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
    ts = pd.to_datetime(ts)
    if getattr(ts.dt, "tz", None) is None:
        ts = ts.dt.tz_localize(tz, ambiguous="infer", nonexistent="shift_forward")
    return ts.dt.tz_convert("UTC").dt.floor("h")


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
    value_cols = [c for c in d.columns
                  if c not in AS_ID_COLS and c != zc and pd.api.types.is_numeric_dtype(d[c])]
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
    if latest_publish and "Publish Time" in d.columns:
        d = d.sort_values("Publish Time").groupby("Interval Start", as_index=False).last()
    d["interval_start_utc"] = to_utc_hour(d["Interval Start"], tz)
    d["value"] = pd.to_numeric(d[value_col], errors="coerce")
    g = d.groupby("interval_start_utc", as_index=False)["value"].mean()
    g.insert(0, "iso", iso)
    g.insert(1, "kind", kind)
    g = add_day_he(g, tz)
    if day is not None:
        g = g[g["day"] == day]
    return g


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

    def lf():
        df = iso.get_load_forecast_day_ahead(date=d0, end=ctx.day(1))
        df = df[df["TAC Area Name"].astype(str).str.upper() == "CA ISO-TAC"]
        return tidy_load(df, name, "forecast", tz, ["Load Forecast"], day=d0)
    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lf))

    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", lambda: tidy_load(
        iso.get_load(date=d2), name, "actual", tz, ["Load"], day=d2)))


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

    def lf():
        df = iso.get_load_forecast(date="latest")
        return tidy_load(df, name, "forecast", tz, ["System Total", "SystemTotal", "Total"], day=d0)
    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lf))

    def la():
        df = iso.get_load_by_weather_zone(date=d2)
        return tidy_load(df, name, "actual", tz, ["System Total", "Total", "Ercot", "ERCOT"],
                         day=d2, latest_publish=False)
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", la))


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

    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: tidy_load(
        iso.get_load_forecast(date="today"), name, "forecast", tz, ["Load Forecast"], day=d0)))
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", lambda: tidy_load(
        iso.get_load(date=d2), name, "actual", tz, ["Load"], day=d2, latest_publish=False)))


def _parse_miso_asm_csv(text: str, day: str, tz: str):
    """Parse MISO ASM MCP market report CSV (layout-agnostic)."""
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
    if he_cols:  # wide by hour: identifier columns + HE1..HE24
        id_cols = [c for c in df.columns if c not in he_cols]
        long = df.melt(id_vars=id_cols, value_vars=he_cols, var_name="HE", value_name="value")
        long["he_n"] = long["HE"].str.extract(r"(\d+)").astype(int)
        zone_col = next((c for c in id_cols if "ZONE" in c.upper()), None)
        prod_col = next((c for c in id_cols if c != zone_col), None)
        long["product"] = long[prod_col].astype(str) if prod_col else "MCP"
        long["zone"] = long[zone_col].astype(str) if zone_col else "System"
    else:  # long by hour: hour column + product columns
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
    return long[["Interval Start", "product", "zone", "value"]]


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

    def lf():
        for day in (d0, d1):
            try:
                df = dfal(day)
            except Exception as e:  # noqa: BLE001
                log.warning("MISO df_al %s not available: %s", day, e)
                continue
            col = next((c for c in df.columns if "MISO" in c.upper() and "MTLF" in c.upper()), None)
            if col is None:
                continue
            out = tidy_load(df.dropna(subset=[col]), name, "forecast", tz, [col], day=d0,
                            latest_publish=False)
            if len(out):
                return out
        return None
    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lf))

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


def fetch_nyiso(ctx: Ctx):
    name, iso = "NYISO", gridstatus.NYISO()
    tz, hubs = iso.default_timezone, HUBS["NYISO"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day: only_day(
            tidy_prices(iso.get_lmp(date=day, market=Markets.DAY_AHEAD_HOURLY),
                        name, "DA", tz, hubs), day)))

    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", lambda: only_day(
        tidy_prices(iso.get_lmp(date=d2, market=Markets.REAL_TIME_HOURLY),
                    name, "RT", tz, hubs), d2)))

    def as_day(day):
        df = iso.get_as_prices_day_ahead_hourly(date=day)
        zone_col = pick_col(df, ["Zone", "Region", "Location"], required=False)
        return only_day(tidy_as_wide(df, name, "DA", tz, zone_col=zone_col), day)
    for label, day in (("D", d0), ("D-1", d1)):
        keep(ctx.as_prices, attempt(ctx, name, f"as_da_{label}", lambda day=day: as_day(day)))

    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: tidy_load(
        iso.get_load_forecast(date=d0), name, "forecast", tz, ["Load Forecast", "NYISO"], day=d0)))
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", lambda: tidy_load(
        iso.get_load(date=d2), name, "actual", tz, ["Load"], day=d2, latest_publish=False)))


def fetch_isone(ctx: Ctx):
    name, iso = "ISONE", gridstatus.ISONE()
    tz, hubs = iso.default_timezone, HUBS["ISONE"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day: only_day(
            tidy_prices(iso.get_lmp_day_ahead_hourly(date=day), name, "DA", tz, hubs), day)))

    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", lambda: only_day(
        tidy_prices(iso.get_lmp_real_time_hourly(date=d2), name, "RT", tz, hubs), d2)))

    def as_rt():
        df = iso.get_reserve_zone_prices_designations_real_time_5_min_final(date=d2)
        zone_col = next((c for c in df.columns if "zone" in c.lower() and "id" not in c.lower()
                         and df[c].dtype == object), None)
        price_cols = [c for c in df.columns if "price" in c.lower()
                      and pd.api.types.is_numeric_dtype(df[c])]
        if not price_cols:
            raise ValueError(f"no price columns in {list(df.columns)}")
        keep_cols = ["Interval Start"] + ([zone_col] if zone_col else []) + price_cols
        return only_day(tidy_as_wide(df[keep_cols], name, "RT", tz, zone_col=zone_col), d2)
    keep(ctx.as_prices, attempt(ctx, name, "as_rt_D-2", as_rt))

    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: tidy_load(
        iso.get_load_forecast(date=d0), name, "forecast", tz, ["Load Forecast"], day=d0)))
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", lambda: tidy_load(
        iso.get_load(date=d2), name, "actual", tz, ["Load"], day=d2, latest_publish=False)))


def fetch_spp(ctx: Ctx):
    name, iso = "SPP", gridstatus.SPP()
    tz, hubs = iso.default_timezone, HUBS["SPP"]
    d0, d1, d2 = ctx.day(0), ctx.day(-1), ctx.day(-2)

    for label, day in (("D", d0), ("D-1", d1), ("D-2", d2)):
        keep(ctx.prices, attempt(ctx, name, f"da_lmp_{label}", lambda day=day: only_day(
            tidy_prices(iso.get_lmp_day_ahead_hourly(date=day, location_type="Hub"),
                        name, "DA", tz, hubs), day)))

    keep(ctx.prices, attempt(ctx, name, "rt_lmp_D-2", lambda: only_day(
        tidy_prices(iso.get_lmp_real_time_5_min_by_location(
            date=d2, location_type="Hub", use_daily_files=True), name, "RT", tz, hubs), d2)))

    for label, day in (("D", d0), ("D-1", d1)):
        keep(ctx.as_prices, attempt(ctx, name, f"as_da_{label}", lambda day=day: only_day(
            tidy_as_wide(iso.get_day_ahead_operating_reserve_prices(date=day), name, "DA", tz,
                         zone_col="Reserve Zone", average_zones=True, zone_label="Zone avg"), day)))

    keep(ctx.load, attempt(ctx, name, "load_forecast_D", lambda: tidy_load(
        iso.get_load_forecast(date=d0), name, "forecast", tz, ["Load Forecast"], day=d0)))
    keep(ctx.load, attempt(ctx, name, "load_actual_D-2", lambda: tidy_load(
        iso.get_load(date=d2), name, "actual", tz, ["Load"], day=d2, latest_publish=False)))


FETCHERS = {
    "CAISO": fetch_caiso, "ERCOT": fetch_ercot, "PJM": fetch_pjm, "MISO": fetch_miso,
    "NYISO": fetch_nyiso, "ISONE": fetch_isone, "SPP": fetch_spp,
}


# --------------------------------------------------------------------------
def default_report_date() -> pd.Timestamp:
    now_bj = datetime.now(timezone.utc) + pd.Timedelta(hours=8)
    return pd.Timestamp(now_bj.date())


def _merge_csv(path: str, new: pd.DataFrame, keys: list[str], cols: list[str]) -> pd.DataFrame:
    """New rows replace old rows sharing the same keys; everything else is kept."""
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

    def dump(frames, name, cols, keys):
        path = os.path.join(out_dir, name)
        if frames:
            df = pd.concat(frames, ignore_index=True)
            df["interval_start_utc"] = df["interval_start_utc"].dt.strftime("%Y-%m-%dT%H:%MZ")
            df["value"] = df["value"].round(3)
            df = df[cols]
        else:
            df = pd.DataFrame(columns=cols)
        df = _merge_csv(path, df, keys, cols)
        df = df.sort_values(cols[:-2]).reset_index(drop=True)
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
        "rows": {"prices": n_p, "as_prices": n_a, "load": n_l},
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
    bad = [iso for iso in ISO_ORDER
           if iso not in status or any(not v.get("ok") for v in status[iso].values())]
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="report date D (YYYY-MM-DD, Beijing). Default: today in Beijing")
    ap.add_argument("--out", default="data", help="output root directory")
    ap.add_argument("--isos", default=",".join(ISO_ORDER), help="comma-separated subset")
    ap.add_argument("--retry-failed", action="store_true",
                    help="only re-fetch ISOs that have a failed dataset in today's meta.json")
    args = ap.parse_args()

    report_date = pd.Timestamp(args.date) if args.date else default_report_date()
    ctx = Ctx(report_date=report_date)
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
