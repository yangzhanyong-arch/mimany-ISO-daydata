#!/usr/bin/env python3
"""
Build summary statistics and charts for the US ISO daily report.

Reads the tidy CSVs written by fetch/fetch_iso.py (data/<D>/ plus the two
previous folders as fallback for DA history) and writes into --out:
  summary.json       all numbers the report and the commentary need
  charts/*.png       overview + per-ISO price / load / AS charts

Usage:
  python3 report/analyze.py --data data --date 2026-11-03 --out work
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

ISO_ORDER = ["CAISO", "ERCOT", "PJM", "MISO", "NYISO", "ISONE", "SPP"]

ISO_INFO = {
    "CAISO": {"label": "CAISO", "cn": "加州独立系统运营商", "tz_cn": "太平洋时间"},
    "ERCOT": {"label": "ERCOT", "cn": "德州电力可靠性委员会", "tz_cn": "美中时间"},
    "PJM": {"label": "PJM", "cn": "PJM 互联", "tz_cn": "美东时间"},
    "MISO": {"label": "MISO", "cn": "中部大陆独立系统运营商", "tz_cn": "美东标准时间(EST)"},
    "NYISO": {"label": "NYISO", "cn": "纽约独立系统运营商", "tz_cn": "美东时间"},
    "ISONE": {"label": "ISO-NE", "cn": "新英格兰独立系统运营商", "tz_cn": "美东时间"},
    "SPP": {"label": "SPP", "cn": "西南电力联营", "tz_cn": "美中时间"},
}
TZ = {"CAISO": "US/Pacific", "ERCOT": "US/Central", "PJM": "US/Eastern", "MISO": "EST",
      "NYISO": "US/Eastern", "ISONE": "US/Eastern", "SPP": "US/Central"}

# Hubs in display order; the first one is the headline hub of the ISO.
HUBS = {
    "CAISO": ["TH_SP15_GEN-APND", "TH_NP15_GEN-APND", "TH_ZP26_GEN-APND"],
    "ERCOT": ["HB_NORTH", "HB_HOUSTON", "HB_SOUTH", "HB_WEST", "HB_HUBAVG"],
    "PJM": ["WESTERN HUB", "AEP-DAYTON HUB", "N ILLINOIS HUB", "EASTERN HUB", "DOMINION HUB"],
    "MISO": ["INDIANA.HUB", "ILLINOIS.HUB", "MICHIGAN.HUB", "MINN.HUB", "LOUISIANA.HUB"],
    "NYISO": ["N.Y.C.", "HUD VL", "CAPITL", "WEST", "LONGIL"],
    "ISONE": [".H.INTERNAL_HUB", ".Z.NEMASSBOST", ".Z.CONNECTICUT", ".Z.MAINE"],
    "SPP": ["SPPNORTH_HUB", "SPPSOUTH_HUB"],
}
HUB_LABELS = {
    "TH_SP15_GEN-APND": "SP15", "TH_NP15_GEN-APND": "NP15", "TH_ZP26_GEN-APND": "ZP26",
    "HB_NORTH": "North Hub", "HB_HOUSTON": "Houston Hub", "HB_SOUTH": "South Hub",
    "HB_WEST": "West Hub", "HB_HUBAVG": "Hub Average",
    "WESTERN HUB": "Western Hub", "AEP-DAYTON HUB": "AEP-Dayton Hub",
    "N ILLINOIS HUB": "N. Illinois Hub", "EASTERN HUB": "Eastern Hub", "DOMINION HUB": "Dominion Hub",
    "INDIANA.HUB": "Indiana Hub", "ILLINOIS.HUB": "Illinois Hub", "MICHIGAN.HUB": "Michigan Hub",
    "MINN.HUB": "Minnesota Hub", "LOUISIANA.HUB": "Louisiana Hub",
    "N.Y.C.": "Zone J (NYC)", "HUD VL": "Zone G (Hudson Vly)", "CAPITL": "Zone F (Capital)",
    "WEST": "Zone A (West)", "LONGIL": "Zone K (Long Isl.)",
    ".H.INTERNAL_HUB": "Mass Hub", ".Z.NEMASSBOST": "NEMA/Boston",
    ".Z.CONNECTICUT": "Connecticut", ".Z.MAINE": "Maine",
    "SPPNORTH_HUB": "North Hub", "SPPSOUTH_HUB": "South Hub",
}

# (regex on product name, Chinese label, sort order)
AS_LABELS = [
    (r"mileage", None, 99),  # dropped
    (r"10 ?min.*non|tmnsr|non-?spin|non-?synchron|nspin|^nr$", "非旋转备用", 7),
    (r"(reg(ulation)?[ _-]?up|regup|^ru$)", "上调频", 1),
    (r"(reg(ulation)?[ _-]?(dn|down)|regdn|^rd$)", "下调频", 2),
    (r"regulation capacity|^reg(ulation)?( mcp)?$|^reg\b|^regulation$|^REG$", "调频", 3),
    (r"responsive|rrs", "响应备用(RRS)", 4),
    (r"ercot contingency|ecrs", "紧急备用(ECRS)", 5),
    (r"10 ?min.*spin|tmsr|\bspin|synchroni[sz]ed reserve|^sr$", "旋转/同步备用", 6),
    (r"primary|^pr$", "一级备用", 8),
    (r"30 ?min|thirty|tmor", "30分钟备用", 9),
    (r"supp", "补充备用", 10),
    (r"\bstr\b|short.?term", "短时备用(STR)", 11),
    (r"ramp.?up|ramp_up", "上爬坡", 12),
    (r"ramp.?(dn|down)|ramp_dn", "下爬坡", 13),
    (r"unc", "不确定性备用", 14),
]

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
REF_GRAY = "#8c8b85"
INK, INK2, MUTED, GRID = "#1b1b1a", "#52514e", "#8a8984", "#e6e5e0"


def as_label(product: str):
    p = str(product)
    for pat, label, order in AS_LABELS:
        if re.search(pat, p, re.I):
            return label, order
    return p, 50


def setup_fonts():
    candidates = ["Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", "SimHei",
                  "Source Han Sans SC", "WenQuanYi Micro Hei", "Arial Unicode MS"]
    installed = {f.name for f in font_manager.fontManager.ttflist}
    family = next((c for c in candidates if c in installed), "DejaVu Sans")
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": [family, "DejaVu Sans"],
        "axes.unicode_minus": False,
        "font.size": 8,
        "axes.titlesize": 9,
        "axes.titleweight": "bold",
        "axes.titlecolor": INK,
        "axes.labelsize": 8,
        "axes.labelcolor": INK2,
        "axes.edgecolor": GRID,
        "axes.linewidth": 0.8,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "xtick.labelsize": 7.5,
        "ytick.labelsize": 7.5,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "grid.linestyle": "-",
        "legend.fontsize": 7.5,
        "legend.frameon": False,
        "figure.dpi": 200,
        "savefig.dpi": 200,
        "savefig.facecolor": "white",
    })
    return family


# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------
def load_tidy(data_root: str, date: str, name: str, keys: list[str]) -> pd.DataFrame:
    """Concatenate data/<D>, data/<D-1>, data/<D-2> files; newest folder wins."""
    frames = []
    for back in range(0, 3):
        d = (pd.Timestamp(date) - timedelta(days=int(back))).strftime("%Y-%m-%d")
        path = os.path.join(data_root, d, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            df = pd.read_csv(path)
            if len(df):
                df["_age"] = back
                frames.append(df)
    if not frames:
        return pd.DataFrame(columns=keys + ["value", "day", "he"])
    df = pd.concat(frames, ignore_index=True).sort_values("_age")
    df = df.drop_duplicates(subset=keys, keep="first").drop(columns="_age")
    df["he"] = df["he"].astype(int)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def r(x, nd=2):
    if x is None:
        return None
    try:
        if isinstance(x, (float, np.floating)) and (np.isnan(x) or np.isinf(x)):
            return None
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def series_stats(s: pd.Series) -> dict | None:
    """s indexed by hour-ending -> stats."""
    s = s.dropna()
    if s.empty:
        return None
    peak = s[(s.index >= 8) & (s.index <= 23)]
    off = s[(s.index < 8) | (s.index > 23)]
    return {
        "avg": r(s.mean()),
        "peak_avg": r(peak.mean()) if len(peak) else None,
        "offpeak_avg": r(off.mean()) if len(off) else None,
        "max": r(s.max()), "max_he": int(s.idxmax()),
        "min": r(s.min()), "min_he": int(s.idxmin()),
        "std": r(s.std()) if len(s) > 1 else None,
        "neg_hours": int((s < 0).sum()),
        "hours": int(len(s)),
    }


def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return r((a - b) / abs(b) * 100, 1)


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------
def _empty(ax, msg="数据暂缺"):
    ax.text(0.5, 0.5, msg, ha="center", va="center", color=MUTED, fontsize=9,
            transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)


def _hour_axis(ax, n=24):
    ticks = [1, 4, 8, 12, 16, 20, 24] if n <= 24 else [1, 5, 9, 13, 17, 21, 25]
    ax.set_xticks(ticks)
    ax.set_xlim(0.5, max(n, 24) + 0.5)
    ax.set_xlabel("小时（HE，当地时间）")


def _zero_line(ax, ymin):
    if ymin is not None and ymin < 0:
        ax.axhline(0, color=INK2, linewidth=0.7, zorder=1)


def _finish(fig, path):
    fig.savefig(path, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)


def chart_prices(iso, pr, d0, d2, path):
    hubs = HUBS[iso]
    fig, axes = plt.subplots(1, 2, figsize=(6.6, 2.2), layout="constrained")
    plotted = False
    ymin = None
    handles = {}
    for ax, (market, day, title) in zip(axes, (("DA", d0, f"日前出清价格（{d0}）"),
                                                ("RT", d2, f"实时出清价格（{d2}，小时均值）"))):
        sub = pr[(pr.iso == iso) & (pr.market == market) & (pr.day == day)]
        ax.set_title(title, loc="left")
        if sub.empty:
            _empty(ax)
            continue
        if market == "RT":
            ref = pr[(pr.iso == iso) & (pr.market == "DA") & (pr.day == day) & (pr.location == hubs[0])]
            if not ref.empty:
                ref = ref.sort_values("he")
                h = ax.plot(ref.he, ref.value, color=REF_GRAY, linewidth=1.2, zorder=2)[0]
                handles[f"{HUB_LABELS.get(hubs[0], hubs[0])} 日前（参考）"] = h
        n = int(sub.he.max())
        for i, hub in enumerate(hubs):
            s = sub[sub.location == hub].sort_values("he")
            if s.empty:
                continue
            h = ax.plot(s.he, s.value, color=PALETTE[i % 8], linewidth=1.5, zorder=3)[0]
            handles.setdefault(HUB_LABELS.get(hub, hub), h)
            plotted = True
            lo = s.value.min()
            ymin = lo if ymin is None else min(ymin, lo)
        _hour_axis(ax, n)
    for ax in axes:
        ax.set_ylabel("$/MWh")
        _zero_line(ax, ymin)
    if plotted:
        # hubs first (fixed order), reference last
        order = [HUB_LABELS.get(h, h) for h in hubs if HUB_LABELS.get(h, h) in handles]
        order += [k for k in handles if k not in order]
        fig.legend([handles[k] for k in order], order, loc="outside lower center",
                   ncol=min(len(order), 6))
    _finish(fig, path)


def chart_load(iso, ld, d0, d2, path):
    fig, ax = plt.subplots(figsize=(3.25, 1.95), layout="constrained")
    ax.set_title("系统负荷（MW）", loc="left")
    fc = ld[(ld.iso == iso) & (ld.kind == "forecast") & (ld.day == d0)].sort_values("he")
    ac = ld[(ld.iso == iso) & (ld.kind == "actual") & (ld.day == d2)].sort_values("he")
    if fc.empty and ac.empty:
        _empty(ax)
        _finish(fig, path)
        return
    hs, labels = [], []
    if not ac.empty:
        hs.append(ax.plot(ac.he, ac.value, color=REF_GRAY, linewidth=1.3)[0])
        labels.append(f"{d2} 实际")
    if not fc.empty:
        hs.append(ax.plot(fc.he, fc.value, color=PALETTE[0], linewidth=1.6)[0])
        labels.append(f"{d0} 预测")
    n = int(max(fc.he.max() if len(fc) else 24, ac.he.max() if len(ac) else 24))
    _hour_axis(ax, n)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v/1000:,.0f}k"))
    ax.legend(hs, labels, loc="best", ncol=1)
    _finish(fig, path)


def chart_as(iso, as_sel, title, path):
    fig, ax = plt.subplots(figsize=(3.25, 1.95), layout="constrained")
    ax.set_title(title, loc="left")
    if as_sel is None or as_sel.empty:
        _empty(ax)
        _finish(fig, path)
        return
    hs, labels = [], []
    order = as_sel.groupby("label")["order"].min().sort_values().index.tolist()
    ymin = None
    for i, lab in enumerate(order[:8]):
        s = as_sel[as_sel.label == lab].sort_values("he")
        hs.append(ax.plot(s.he, s.value, color=PALETTE[i], linewidth=1.4)[0])
        labels.append(lab)
        ymin = s.value.min() if ymin is None else min(ymin, s.value.min())
    _zero_line(ax, ymin)
    _hour_axis(ax, int(as_sel.he.max()))
    ax.set_ylabel("$/MW·h")
    ax.legend(hs, labels, loc="best", ncol=2 if len(labels) > 3 else 1, fontsize=6.6)
    _finish(fig, path)


def chart_overview_bar(rows, d0, d1, path):
    rows = [x for x in rows if x.get("da_avg") is not None]
    fig, ax = plt.subplots(figsize=(6.6, 2.5), layout="constrained")
    ax.set_title("各ISO主枢纽日前均价（$/MWh）", loc="left")
    if not rows:
        _empty(ax)
        _finish(fig, path)
        return
    x = np.arange(len(rows))
    w = 0.36
    prev = [x_.get("da_prev_avg") for x_ in rows]
    cur = [x_["da_avg"] for x_ in rows]
    b1 = ax.bar(x - w / 2 - 0.01, [p if p is not None else 0 for p in prev], w,
                color="#c9c8c2", label=f"{d1}")
    b2 = ax.bar(x + w / 2 + 0.01, cur, w, color=PALETTE[0], label=f"{d0}")
    for rect, v in zip(b2, cur):
        ax.text(rect.get_x() + rect.get_width() / 2, rect.get_height(), f"{v:.1f}",
                ha="center", va="bottom", fontsize=7, color=INK)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{ISO_INFO[x_['iso']]['label']}\n{x_['main_hub']}" for x_ in rows], fontsize=7)
    ax.grid(axis="x", visible=False)
    ax.axhline(0, color=INK2, linewidth=0.7)
    fig.legend(handles=[b1, b2], labels=[f"{d1}", f"{d0}"], loc="outside lower center", ncol=2)
    ax.margins(y=0.12)
    _finish(fig, path)


def chart_overview_profiles(pr, d0, path):
    fig, ax = plt.subplots(figsize=(6.6, 2.7), layout="constrained")
    ax.set_title(f"各ISO主枢纽日前逐时价格（{d0}，各自当地时间，$/MWh）", loc="left")
    hs, labels = [], []
    ymin = None
    for i, iso in enumerate(ISO_ORDER):
        hub = HUBS[iso][0]
        s = pr[(pr.iso == iso) & (pr.market == "DA") & (pr.day == d0) & (pr.location == hub)].sort_values("he")
        if s.empty:
            continue
        hs.append(ax.plot(s.he, s.value, color=PALETTE[i], linewidth=1.5)[0])
        labels.append(f"{ISO_INFO[iso]['label']} {HUB_LABELS.get(hub, hub)}")
        ymin = s.value.min() if ymin is None else min(ymin, s.value.min())
    if not hs:
        _empty(ax)
        _finish(fig, path)
        return
    _zero_line(ax, ymin)
    _hour_axis(ax, 24)
    fig.legend(hs, labels, loc="outside lower center", ncol=4)
    _finish(fig, path)


# --------------------------------------------------------------------------
def pick_as_zone(iso, sub):
    zones = sub.zone.astype(str).unique().tolist()
    if not zones:
        return None
    prefs = {"PJM": ["RTO"], "NYISO": ["EAST"], "ISONE": ["ROS", "REST", "SYSTEM"],
             "CAISO": ["CAISO"], "MISO": [], "ERCOT": [], "SPP": []}.get(iso, [])
    for p in prefs:
        for z in zones:
            if p.upper() in z.upper():
                return z
    return zones[0]


def build(args):
    D = pd.Timestamp(args.date)
    d0, d1, d2 = (D.strftime("%Y-%m-%d"), (D - timedelta(days=1)).strftime("%Y-%m-%d"),
                  (D - timedelta(days=2)).strftime("%Y-%m-%d"))
    out = args.out
    os.makedirs(os.path.join(out, "charts"), exist_ok=True)
    family = setup_fonts()

    pr = load_tidy(args.data, d0, "prices.csv", ["iso", "market", "location", "interval_start_utc"])
    asp = load_tidy(args.data, d0, "as_prices.csv", ["iso", "market", "product", "zone", "interval_start_utc"])
    ld = load_tidy(args.data, d0, "load.csv", ["iso", "kind", "interval_start_utc"])
    # Only today's folder may hold load data for D / D-2 with the right vintage
    meta_path = os.path.join(args.data, d0, "meta.json")
    meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else {}

    if len(asp):
        lab = asp["product"].map(as_label)
        asp["label"] = [x[0] for x in lab]
        asp["order"] = [x[1] for x in lab]
        asp = asp[asp["label"].notna()]

    summary = {
        "report_date": d0, "da_day": d0, "da_prev_day": d1, "rt_day": d2,
        "generated_at_bj": (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
        "data_fetched_at_utc": meta.get("generated_at_utc"),
        "chart_font": family,
        "isos": {}, "overview": [], "data_issues": [],
    }

    for iso in ISO_ORDER:
        info = ISO_INFO[iso]
        hubs = HUBS[iso]
        st = (meta.get("status") or {}).get(iso, {})
        failed = [k for k, v in st.items() if not v.get("ok")]
        for k in failed:
            summary["data_issues"].append(f"{info['label']}: {k} 获取失败 - {st[k].get('error', '')[:160]}")

        hub_rows = []
        for hub in hubs:
            da0 = pr[(pr.iso == iso) & (pr.market == "DA") & (pr.day == d0) & (pr.location == hub)].set_index("he")["value"]
            da1 = pr[(pr.iso == iso) & (pr.market == "DA") & (pr.day == d1) & (pr.location == hub)].set_index("he")["value"]
            da2 = pr[(pr.iso == iso) & (pr.market == "DA") & (pr.day == d2) & (pr.location == hub)].set_index("he")["value"]
            rt2 = pr[(pr.iso == iso) & (pr.market == "RT") & (pr.day == d2) & (pr.location == hub)].set_index("he")["value"]
            s0, s1, s2, srt = series_stats(da0), series_stats(da1), series_stats(da2), series_stats(rt2)
            if not any([s0, s1, s2, srt]):
                continue
            row = {"id": hub, "label": HUB_LABELS.get(hub, hub), "da": s0, "da_prev": s1,
                   "da_d2": s2, "rt": srt}
            row["da_chg"] = r(s0["avg"] - s1["avg"]) if s0 and s1 else None
            row["da_chg_pct"] = pct(s0["avg"], s1["avg"]) if s0 and s1 else None
            row["da_minus_rt"] = r(s2["avg"] - srt["avg"]) if s2 and srt else None
            if s2 is not None and srt is not None:
                both = pd.concat([da2, rt2], axis=1, keys=["da", "rt"]).dropna()
                if len(both):
                    diff = both["rt"] - both["da"]
                    row["rt_minus_da_max"] = r(diff.max())
                    row["rt_minus_da_max_he"] = int(diff.idxmax())
            hub_rows.append(row)

        # ancillary services
        as_block = None
        sub = asp[asp.iso == iso] if len(asp) else asp
        if len(sub):
            market = "DA" if (sub.market == "DA").any() else "RT"
            sub = sub[sub.market == market]
            as_day = d0 if market == "DA" else d2
            if iso in ("MISO",) and sub.zone.nunique() > 1:
                sub = sub.groupby(["product", "label", "order", "day", "he"], as_index=False)["value"].mean()
                sub["zone"] = "各备用区平均"
            zone = pick_as_zone(iso, sub)
            sub = sub[sub.zone == zone]
            cur = sub[sub.day == as_day]
            prev = sub[sub.day == d1] if market == "DA" else sub.iloc[0:0]
            prods = []
            for lab in cur.groupby("label")["order"].min().sort_values().index:
                c = cur[cur.label == lab].groupby("he")["value"].mean()
                p = prev[prev.label == lab].groupby("he")["value"].mean() if len(prev) else pd.Series(dtype=float)
                cs, ps = series_stats(c), series_stats(p)
                prods.append({"label": lab, "avg": cs["avg"] if cs else None,
                              "max": cs["max"] if cs else None, "max_he": cs["max_he"] if cs else None,
                              "prev_avg": ps["avg"] if ps else None,
                              "chg": r(cs["avg"] - ps["avg"]) if cs and ps else None})
            as_block = {"market": market, "day": as_day, "zone": zone, "products": prods}
            chart_title = ("日前辅助服务出清价" if market == "DA" else "实时备用出清价（小时均值）")
            chart_title += f"（{as_day}）"
            chart_as(iso, cur, chart_title, os.path.join(out, "charts", f"{iso}_as.png"))
        else:
            chart_as(iso, None, "辅助服务出清价", os.path.join(out, "charts", f"{iso}_as.png"))

        # load
        fc = ld[(ld.iso == iso) & (ld.kind == "forecast") & (ld.day == d0)].set_index("he")["value"] if len(ld) else pd.Series(dtype=float)
        ac = ld[(ld.iso == iso) & (ld.kind == "actual") & (ld.day == d2)].set_index("he")["value"] if len(ld) else pd.Series(dtype=float)
        fs, as_ = series_stats(fc), series_stats(ac)
        load_block = {
            "forecast": {"day": d0, "peak": r(fs["max"], 0), "peak_he": fs["max_he"], "avg": r(fs["avg"], 0),
                         "min": r(fs["min"], 0), "min_he": fs["min_he"]} if fs else None,
            "actual": {"day": d2, "peak": r(as_["max"], 0), "peak_he": as_["max_he"], "avg": r(as_["avg"], 0),
                       "min": r(as_["min"], 0), "min_he": as_["min_he"]} if as_ else None,
        }
        if fs and as_:
            load_block["peak_chg_pct"] = pct(fs["max"], as_["max"])

        chart_prices(iso, pr, d0, d2, os.path.join(out, "charts", f"{iso}_price.png"))
        chart_load(iso, ld, d0, d2, os.path.join(out, "charts", f"{iso}_load.png"))

        main = next((h for h in hub_rows if h["id"] == hubs[0]), hub_rows[0] if hub_rows else None)
        flags = []
        for h in hub_rows:
            if h["rt"] and h["rt"]["avg"] and h["rt"]["max"] is not None and h["rt"]["max"] > max(3 * abs(h["rt"]["avg"]), 150):
                flags.append(f"{h['label']} 实时最高 {h['rt']['max']} 出现在 HE{h['rt']['max_he']}，显著高于日均 {h['rt']['avg']}")
            for key, nm in (("da", "日前"), ("rt", "实时")):
                if h[key] and h[key]["neg_hours"]:
                    flags.append(f"{h['label']} {nm}出现 {h[key]['neg_hours']} 个负电价小时（最低 {h[key]['min']}，HE{h[key]['min_he']}）")
            if h["da_chg_pct"] is not None and abs(h["da_chg_pct"]) >= 20:
                flags.append(f"{h['label']} 日前均价较前日变化 {h['da_chg_pct']}%")
            if h.get("da_minus_rt") is not None and h["da"] and abs(h["da_minus_rt"]) >= 10:
                flags.append(f"{h['label']} {d2} 日前-实时均价差 {h['da_minus_rt']} $/MWh")

        summary["isos"][iso] = {
            "label": info["label"], "name_cn": info["cn"], "tz": TZ[iso], "tz_cn": info["tz_cn"],
            "main_hub": HUB_LABELS.get(hubs[0], hubs[0]),
            "hubs": hub_rows, "as": as_block, "load": load_block, "flags": flags,
            "failed_datasets": failed,
            "charts": {k: f"charts/{iso}_{k}.png" for k in ("price", "load", "as")},
        }
        summary["overview"].append({
            "iso": iso, "label": info["label"], "main_hub": HUB_LABELS.get(hubs[0], hubs[0]),
            "da_avg": main["da"]["avg"] if main and main["da"] else None,
            "da_prev_avg": main["da_prev"]["avg"] if main and main["da_prev"] else None,
            "da_chg_pct": main["da_chg_pct"] if main else None,
            "da_max": main["da"]["max"] if main and main["da"] else None,
            "da_max_he": main["da"]["max_he"] if main and main["da"] else None,
            "rt_avg": main["rt"]["avg"] if main and main["rt"] else None,
            "rt_max": main["rt"]["max"] if main and main["rt"] else None,
            "da_minus_rt": main["da_minus_rt"] if main else None,
            "load_fc_peak": load_block["forecast"]["peak"] if load_block["forecast"] else None,
            "load_act_peak": load_block["actual"]["peak"] if load_block["actual"] else None,
        })

    chart_overview_bar(summary["overview"], d0, d1, os.path.join(out, "charts", "overview_da_bar.png"))
    chart_overview_profiles(pr, d0, os.path.join(out, "charts", "overview_da_profiles.png"))
    summary["charts"] = {"overview_bar": "charts/overview_da_bar.png",
                         "overview_profiles": "charts/overview_da_profiles.png"}

    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    n_ok = sum(1 for o in summary["overview"] if o["da_avg"] is not None)
    print(f"summary written: {os.path.join(out, 'summary.json')} | ISOs with DA data: {n_ok}/7 | "
          f"issues: {len(summary['data_issues'])} | font: {family}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--date", required=True, help="report date D (YYYY-MM-DD)")
    ap.add_argument("--out", default="work")
    build(ap.parse_args())


if __name__ == "__main__":
    main()
