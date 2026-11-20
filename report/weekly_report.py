#!/usr/bin/env python3
"""
Build weekly US ISO market summary, charts, and Markdown report.

For a Monday report date D (Beijing calendar date):
  * Review week (complete RT / Actual / Mix): [D-8 .. D-2] (Sun .. Sat, 7 US operating days)
  * Previous week (WoW baseline):            [D-15 .. D-9]
  * Early-week DA outlook:                   [D-1 .. D]

Outputs under --out (default reports/weekly/<D>):
  weekly_summary.json
  weekly_report.md
  charts/overview_weekly_bar.png
  charts/overview_dart_weekly.png
  charts/overview_basis.png
  charts/overview_mix_share.png
  charts/<ISO>_weekly.png
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

ISO_ORDER = ["CAISO", "ERCOT", "PJM", "MISO", "NYISO", "ISONE", "SPP"]

ISO_INFO = {
    "CAISO": {"label": "CAISO", "cn": "加州独立系统运营商"},
    "ERCOT": {"label": "ERCOT", "cn": "德州电力可靠性委员会"},
    "PJM": {"label": "PJM", "cn": "PJM 互联"},
    "MISO": {"label": "MISO", "cn": "中部大陆独立系统运营商"},
    "NYISO": {"label": "NYISO", "cn": "纽约独立系统运营商"},
    "ISONE": {"label": "ISO-NE", "cn": "新英格兰独立系统运营商"},
    "SPP": {"label": "SPP", "cn": "西南电力联营"},
}

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
    "HB_NORTH": "North", "HB_HOUSTON": "Houston", "HB_SOUTH": "South",
    "HB_WEST": "West", "HB_HUBAVG": "Hub Avg",
    "WESTERN HUB": "Western", "AEP-DAYTON HUB": "AEP-Dayton",
    "N ILLINOIS HUB": "N. Illinois", "EASTERN HUB": "Eastern", "DOMINION HUB": "Dominion",
    "INDIANA.HUB": "Indiana", "ILLINOIS.HUB": "Illinois", "MICHIGAN.HUB": "Michigan",
    "MINN.HUB": "Minnesota", "LOUISIANA.HUB": "Louisiana",
    "N.Y.C.": "Zone J(NYC)", "HUD VL": "Zone G", "CAPITL": "Zone F",
    "WEST": "Zone A(West)", "LONGIL": "Zone K",
    ".H.INTERNAL_HUB": "Mass Hub", ".Z.NEMASSBOST": "NEMA/Boston",
    ".Z.CONNECTICUT": "Connecticut", ".Z.MAINE": "Maine",
    "SPPNORTH_HUB": "North", "SPPSOUTH_HUB": "South",
}

BASIS_PAIRS = {
    "CAISO": ("TH_SP15_GEN-APND", "TH_NP15_GEN-APND", "SP15 - NP15"),
    "ERCOT": ("HB_HOUSTON", "HB_WEST", "Houston - West"),
    "PJM": ("DOMINION HUB", "WESTERN HUB", "Dominion - Western"),
    "MISO": ("LOUISIANA.HUB", "MINN.HUB", "Louisiana - Minn."),
    "NYISO": ("N.Y.C.", "WEST", "NYC(J) - West(A)"),
    "ISONE": (".Z.NEMASSBOST", ".Z.MAINE", "Boston - Maine"),
    "SPP": ("SPPSOUTH_HUB", "SPPNORTH_HUB", "South - North"),
}

FUEL_GROUPS = [
    (r"solar|photovoltaic", "光伏"),
    (r"wind", "风电"),
    (r"nuclear", "核电"),
    (r"natural gas|gas\b|dual fuel", "天然气"),
    (r"coal|lignite", "燃煤"),
    (r"hydro", "水电"),
    (r"batter|storage", "储能"),
    (r"import", "净进口"),
]
FUEL_ORDER = ["核电", "燃煤", "水电", "天然气", "风电", "光伏", "储能", "净进口", "其他"]
FUEL_COLORS = {
    "核电": "#7a68c9", "燃煤": "#6e6b65", "天然气": "#eda100",
    "水电": "#2a78d6", "风电": "#1baf7a", "光伏": "#f2c94c",
    "储能": "#e87ba4", "净进口": "#999999", "其他": "#c9c8c2",
}

AS_LABELS = [
    (r"mileage", None, 99),
    (r"10 ?min.*non|tmnsr|non ?spin|non ?synchron|nspin|^nr$", "非旋转备用", 7),
    (r"reg(ulation)? ?up|regup|^ru$", "上调频", 1),
    (r"reg(ulation)? ?(dn|down)|regdn|regdown|^rd$", "下调频", 2),
    (r"^reg(ulation)?( capacity)?$|^reg\b", "调频", 3),
    (r"responsive|rrs", "响应备用(RRS)", 4),
    (r"ercot contingency|ecrs", "紧急备用(ECRS)", 5),
    (r"10 ?min.*spin|tmsr|spin|synchroni[sz]ed|^sr$", "旋转/同步备用", 6),
    (r"^tmr\b|^ten ?min(ute)? reserve", "10分钟备用(TMR)", 8),
    (r"30 ?min|thirty|tmor", "30分钟备用", 9),
    (r"^str\b|\bstr\b|short ?term", "短时备用(STR)", 11),
]

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK2, MUTED, GRID = "#1b1b1a", "#52514e", "#8a8984", "#e6e5e0"


def setup_fonts():
    candidates = ["Noto Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC",
                  "SimHei", "Source Han Sans SC", "WenQuanYi Micro Hei", "Arial Unicode MS"]
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
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "legend.fontsize": 7.3,
        "legend.frameon": False,
        "figure.dpi": 200,
        "savefig.dpi": 200,
        "savefig.facecolor": "white",
    })
    return family


def fuel_group(fuel: str) -> str:
    f = str(fuel).strip().lower()
    for pat, label in FUEL_GROUPS:
        if re.search(pat, f, re.I):
            return label
    return "其他"


def as_label(product: str):
    n = re.sub(r"[_\-]+", " ", str(product).strip().lower())
    n = re.sub(r"\b(clearing price|price|mcpc|mcp)\b", "", n)
    n = re.sub(r"mcp$", "", n).strip()
    for pat, label, order in AS_LABELS:
        if re.search(pat, n, re.I):
            return label, order
    return str(product).strip(), 50


def r(x, nd=2):
    if x is None:
        return None
    try:
        if isinstance(x, (float, np.floating)) and (np.isnan(x) or np.isinf(x)):
            return None
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def pct(cur, prev):
    if cur is None or prev is None or prev == 0:
        return None
    return r((cur - prev) / abs(prev) * 100, 1)


def fmt(v, nd=1, plus=False, suffix=""):
    if v is None:
        return "—"
    s = f"{v:+.{nd}f}" if plus else f"{v:.{nd}f}"
    return s + suffix


def load_window(data_root: str, report_date: str, name: str, keys: list[str],
                days_back: int = 20, has_he: bool = True) -> pd.DataFrame:
    """Load and deduplicate CSVs across data/<D-days_back>..data/<D>; newest folder wins."""
    D = pd.Timestamp(report_date)
    frames = []
    for back in range(0, days_back + 1):
        d = (D - timedelta(days=back)).strftime("%Y-%m-%d")
        path = os.path.join(data_root, d, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                df = pd.read_csv(path, dtype={"day": str})
                if len(df):
                    df["_age"] = back
                    frames.append(df)
            except Exception:  # noqa: BLE001
                continue
    if not frames:
        cols = keys + ["value", "day"] + (["he"] if has_he else [])
        return pd.DataFrame(columns=cols)
    df = pd.concat(frames, ignore_index=True).sort_values("_age")
    df = df.drop_duplicates(subset=keys, keep="first").drop(columns="_age")
    if has_he and "he" in df.columns:
        df["he"] = pd.to_numeric(df["he"], errors="coerce").fillna(0).astype(int)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    if "interval_start_utc" in df.columns:
        df["ts"] = pd.to_datetime(df["interval_start_utc"], utc=True, errors="coerce")
    return df


def day_range(start_dt: pd.Timestamp, n_days: int) -> list[str]:
    return [(start_dt + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n_days)]


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------
def _finish(fig, path):
    fig.savefig(path, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)


def chart_weekly_bar(rows, title_range, path):
    valid = [x for x in rows if x.get("rt_avg") is not None or x.get("da_avg") is not None]
    fig, ax = plt.subplots(figsize=(7.0, 2.6), layout="constrained")
    ax.set_title(f"各ISO主枢纽周均电价对比（{title_range}，$/MWh）", loc="left")
    if not valid:
        ax.text(0.5, 0.5, "数据暂缺", ha="center", va="center", transform=ax.transAxes)
        _finish(fig, path)
        return
    x = np.arange(len(valid))
    w = 0.27
    rt_prev = [x_.get("rt_prev_avg") or 0 for x_ in valid]
    rt_cur = [x_.get("rt_avg") or 0 for x_ in valid]
    da_cur = [x_.get("da_avg") or 0 for x_ in valid]
    b1 = ax.bar(x - w - 0.01, rt_prev, w, color="#c9c8c2", label="上周 RT 均价")
    b2 = ax.bar(x, rt_cur, w, color=PALETTE[0], label="本周 RT 均价")
    b3 = ax.bar(x + w + 0.01, da_cur, w, color=PALETTE[1], alpha=0.85, label="本周 DA 均价")
    for rect, v in zip(b2, [x_.get("rt_avg") for x_ in valid]):
        if v is not None:
            ax.text(rect.get_x() + rect.get_width() / 2, rect.get_height(), f"{v:.1f}",
                    ha="center", va="bottom", fontsize=6.6, color=INK)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{x_['label']}\n{x_['main_hub']}" for x_ in valid], fontsize=7)
    ax.grid(axis="x", visible=False)
    ax.axhline(0, color=INK2, linewidth=0.7)
    ax.margins(y=0.14)
    fig.legend(handles=[b1, b2, b3], loc="outside lower center", ncol=3)
    _finish(fig, path)


def chart_dart_weekly(rows, title_range, path):
    valid = [x for x in rows if x.get("dart_avg") is not None]
    fig, ax = plt.subplots(figsize=(7.0, 2.3), layout="constrained")
    ax.set_title(f"周度 DART 价差（DA - RT）与波动区间（{title_range}，$/MWh）", loc="left")
    if not valid:
        ax.text(0.5, 0.5, "数据暂缺", ha="center", va="center", transform=ax.transAxes)
        _finish(fig, path)
        return
    x = np.arange(len(valid))
    vals = [x_["dart_avg"] for x_ in valid]
    colors = [PALETTE[2] if v >= 0 else PALETTE[7] for v in vals]
    bars = ax.bar(x, vals, width=0.45, color=colors, alpha=0.88)
    ax.axhline(0, color=INK2, linewidth=0.8)
    for rect, v in zip(bars, vals):
        va = "bottom" if v >= 0 else "top"
        ax.text(rect.get_x() + rect.get_width() / 2, v, f"{v:+.1f}", ha="center", va=va, fontsize=7.2)
    ax.set_xticks(x)
    ax.set_xticklabels([x_["label"] for x_ in valid])
    ax.grid(axis="x", visible=False)
    ax.margins(y=0.2)
    _finish(fig, path)


def chart_basis(basis_rows, title_range, path):
    valid = [b for b in basis_rows if b.get("cur_avg") is not None]
    fig, ax = plt.subplots(figsize=(7.0, 2.3), layout="constrained")
    ax.set_title(f"各ISO内部核心区域阻塞基差（RT均价差，{title_range}，$/MWh）", loc="left")
    if not valid:
        ax.text(0.5, 0.5, "数据暂缺", ha="center", va="center", transform=ax.transAxes)
        _finish(fig, path)
        return
    x = np.arange(len(valid))
    w = 0.35
    prev = [b.get("prev_avg") or 0 for b in valid]
    cur = [b["cur_avg"] for b in valid]
    b1 = ax.bar(x - w / 2 - 0.01, prev, w, color="#c9c8c2", label="上周基差")
    b2 = ax.bar(x + w / 2 + 0.01, cur, w, color=PALETTE[3], label="本周基差")
    ax.axhline(0, color=INK2, linewidth=0.7)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{b['iso_label']}\n{b['pair']}" for b in valid], fontsize=6.8)
    ax.grid(axis="x", visible=False)
    fig.legend(handles=[b1, b2], loc="outside lower center", ncol=2)
    _finish(fig, path)


def chart_mix_share(mix_rows, title_range, path):
    valid = [m for m in mix_rows if m.get("share_pct")]
    fig, ax = plt.subplots(figsize=(7.0, 2.45), layout="constrained")
    ax.set_title(f"各ISO周度发电能源结构占比（{title_range}，%）", loc="left")
    if not valid:
        ax.text(0.5, 0.5, "电源结构数据暂缺（需累计燃料混合数据）", ha="center", va="center",
                color=MUTED, transform=ax.transAxes)
        _finish(fig, path)
        return
    labels = [m["label"] for m in valid]
    y = np.arange(len(valid))
    left = np.zeros(len(valid))
    present = [g for g in FUEL_ORDER if any(m["share_pct"].get(g, 0) > 0 for m in valid)]
    for g in present:
        vals = np.array([max(m["share_pct"].get(g, 0.0), 0.0) for m in valid])
        ax.barh(y, vals, left=left, height=0.55, color=FUEL_COLORS.get(g, "#ccc"), label=g)
        left += vals
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlim(0, 100)
    ax.set_xlabel("发电量占比 (%)")
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    fig.legend(loc="outside lower center", ncol=min(len(present), 5))
    _finish(fig, path)


def chart_iso_weekly(iso, pr_sub, fm_sub, ld_sub, hub, path):
    fig, axes = plt.subplots(2, 1, figsize=(7.0, 3.8), layout="constrained", sharex=False)
    label = ISO_INFO[iso]["label"]
    hub_lb = HUB_LABELS.get(hub, hub)

    # Top: 168h DA vs RT prices
    ax = axes[0]
    ax.set_title(f"{label} ({hub_lb}) — 过去7天逐小时日前 vs 实时电价（$/MWh）", loc="left")
    da = pr_sub[(pr_sub.market == "DA") & (pr_sub.location == hub)].sort_values("ts")
    rt = pr_sub[(pr_sub.market == "RT") & (pr_sub.location == hub)].sort_values("ts")
    if da.empty and rt.empty:
        ax.text(0.5, 0.5, "电价数据暂缺", ha="center", va="center", color=MUTED, transform=ax.transAxes)
    else:
        if not da.empty:
            ax.plot(da["ts"], da["value"], color="#7c7b75", linewidth=1.1, label="日前(DA)")
        if not rt.empty:
            ax.plot(rt["ts"], rt["value"], color=PALETTE[0], linewidth=1.25, alpha=0.92, label="实时(RT)")
        ax.axhline(0, color=INK2, linewidth=0.6)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        ax.set_ylabel("$/MWh")
        ax.legend(loc="upper right", ncol=2)

    # Bottom: 168h Fuel Mix + Net Load
    ax2 = axes[1]
    ax2.set_title(f"{label} — 过去7天电源结构与负荷/净负荷走势（MW）", loc="left")
    if fm_sub.empty and ld_sub.empty:
        ax2.text(0.5, 0.5, "电源结构与负荷数据暂缺", ha="center", va="center", color=MUTED, transform=ax2.transAxes)
    else:
        if not fm_sub.empty:
            piv = fm_sub.pivot_table(index="ts", columns="group", values="value", aggfunc="sum").sort_index().fillna(0)
            cols = [c for c in FUEL_ORDER if c in piv.columns and (piv[c] >= 0).all() and piv[c].sum() > 0]
            if cols:
                ax2.stackplot(piv.index, [piv[c].values for c in cols], labels=cols,
                              colors=[FUEL_COLORS.get(c, "#ccc") for c in cols], alpha=0.8)
        if not ld_sub.empty:
            ac = ld_sub[ld_sub.kind == "actual"].sort_values("ts")
            if not ac.empty:
                ax2.plot(ac["ts"], ac["value"], color=INK, linewidth=1.2, label="实际负荷")
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        ax2.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v/1000:,.0f}k"))
        ax2.set_ylabel("MW")
        ax2.legend(loc="upper right", ncol=5, fontsize=6.5)

    _finish(fig, path)


# --------------------------------------------------------------------------
# core computation
# --------------------------------------------------------------------------
def price_block(df: pd.DataFrame, market: str, hub: str, days: list[str]) -> dict | None:
    sub = df[(df.market == market) & (df.location == hub) & (df.day.isin(days))]
    if sub.empty:
        return None
    s = sub["value"].dropna()
    if s.empty:
        return None
    pk = sub[(sub.he >= 8) & (sub.he <= 23)]["value"].dropna()
    off = sub[(sub.he < 8) | (sub.he > 23)]["value"].dropna()
    max_row = sub.loc[s.idxmax()]
    min_row = sub.loc[s.idxmin()]
    return {
        "avg": r(s.mean()),
        "peak_avg": r(pk.mean()) if len(pk) else None,
        "offpeak_avg": r(off.mean()) if len(off) else None,
        "max": r(s.max()),
        "max_at": f"{max_row['day']} HE{int(max_row['he'])}",
        "min": r(s.min()),
        "min_at": f"{min_row['day']} HE{int(min_row['he'])}",
        "std": r(s.std()) if len(s) > 1 else None,
        "neg_hours": int((s < 0).sum()),
        "spike_100_hours": int((s >= 100).sum()),
        "days": int(sub["day"].nunique()),
        "hours": int(len(s)),
    }


def render_markdown(summary: dict) -> str:
    w_start, w_end = summary["review_week"]["start"], summary["review_week"]["end"]
    p_start, p_end = summary["prev_week"]["start"], summary["prev_week"]["end"]
    d0 = summary["report_date"]

    lines = [
        f"# 美国七大电力市场（ISO/RTO）运行周报（{w_start} 至 {w_end}）",
        "",
        f"> **报告生成日（北京时间）**：{d0} | **完整复盘周（美国运行日）**：`{w_start}`（周日）至 `{w_end}`（周六） | **环比基期**：`{p_start}` 至 `{p_end}`",
        "> *注：周一北京时间下午生成时，美国周六已完成全天 RT 结算；峰时（On-Peak）统一按当地 HE8–23 口径测算。*",
        "",
        "## 一、 本周全美电力市场核心摘要",
        "",
    ]
    for b in summary.get("highlights", []):
        lines.append(f"- {b}")

    lines += [
        "",
        "## 二、 跨区周度核心指标总览",
        "",
        "| ISO (主枢纽) | 本周RT均价 ($/MWh) | RT周环比 | 本周DA均价 | Peak / Off-Peak (RT) | DART (DA-RT) | 负电价小时 | 周均负荷 (GW) | 负荷环比 | 负荷预测偏差 | 新能源占比 |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for o in summary["overview"]:
        pk_off = f"{fmt(o['rt_peak_avg'], 1)} / {fmt(o['rt_offpeak_avg'], 1)}" if o["rt_peak_avg"] is not None else "—"
        ld_gw = fmt(o["load_avg_mw"] / 1000, 1) if o["load_avg_mw"] is not None else "—"
        ren = fmt(o["ren_share_pct"], 1, suffix="%") if o["ren_share_pct"] is not None else "—"
        lines.append(
            f"| **{o['label']}** ({o['main_hub']}) | {fmt(o['rt_avg'], 1)} | {fmt(o['rt_wow_pct'], 1, True, '%')} | "
            f"{fmt(o['da_avg'], 1)} | {pk_off} | {fmt(o['dart_avg'], 1, True)} | "
            f"{o['rt_neg_hours'] if o['rt_neg_hours'] is not None else '—'} | {ld_gw} | "
            f"{fmt(o['load_wow_pct'], 1, True, '%')} | {fmt(o['load_err_pct'], 1, True, '%')} | {ren} |"
        )

    lines += [
        "",
        "![各ISO周均电价对比](charts/overview_weekly_bar.png)",
        "![周度DART价差](charts/overview_dart_weekly.png)",
        "![区域阻塞基差](charts/overview_basis.png)",
        "![电源结构占比](charts/overview_mix_share.png)",
        "",
        "## 三、 空间阻塞基差与燃料基准",
        "",
        "| ISO | 监测基差对 | 本周均价差 ($/MWh) | 上周均价差 | 周变化 ($/MWh) | 解读 |",
        "| :--- | :--- | :---: | :---: | :---: | :--- |",
    ]
    for b in summary.get("basis", []):
        chg = r(b["cur_avg"] - b["prev_avg"]) if b["cur_avg"] is not None and b["prev_avg"] is not None else None
        note = "阻塞显著扩大" if (chg is not None and abs(chg) >= 5) else ("基差平稳" if chg is not None else "数据不足")
        lines.append(f"| **{b['iso_label']}** | {b['pair']} | {fmt(b['cur_avg'], 2, True)} | {fmt(b['prev_avg'], 2, True)} | {fmt(chg, 2, True)} | {note} |")

    if summary.get("gas_weekly"):
        lines += [
            "",
            "### 天然气参考价格周均值（$/MMBtu）",
            "",
            "| 气价枢纽 | 本周均值 | 上周均值 | 周环比 |",
            "| :--- | :---: | :---: | :---: |",
        ]
        for g in summary["gas_weekly"]:
            lines.append(f"| **{g['hub']}** | {fmt(g['cur'], 3)} | {fmt(g['prev'], 3)} | {fmt(g['wow_pct'], 1, True, '%')} |")

    lines += [
        "",
        "## 四、 周初开盘前瞻（周日 & 周一日前市场 DA）",
        "",
        "| ISO (主枢纽) | 周日 DA 均价 | 周一 DA 均价 | 周一 vs 上周DA均值 | 周一预测峰值负荷 (GW) |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ]
    for f in summary.get("early_week_outlook", []):
        pk_gw = fmt(f["mon_load_peak_mw"] / 1000, 1) if f["mon_load_peak_mw"] is not None else "—"
        lines.append(
            f"| **{f['label']}** ({f['main_hub']}) | {fmt(f['sun_da_avg'], 1)} | {fmt(f['mon_da_avg'], 1)} | "
            f"{fmt(f['mon_vs_last_da_pct'], 1, True, '%')} | {pk_gw} |"
        )

    lines += ["", "## 五、 七大 ISO 分区详述（168小时走势）", ""]
    for iso in ISO_ORDER:
        d = summary["isos"][iso]
        lines += [
            f"### {d['label']}（{d['name_cn']}）",
            "",
            f"- **主力枢纽 ({d['main_hub']})**：本周 RT 均价 **{fmt(d['rt']['avg'] if d['rt'] else None, 2)} $/MWh**"
            f"（环比 {fmt(d['rt_wow_pct'], 1, True, '%')}），峰值 **{fmt(d['rt']['max'] if d['rt'] else None, 2)}**"
            f"（{d['rt']['max_at'] if d['rt'] else '—'}），谷值 **{fmt(d['rt']['min'] if d['rt'] else None, 2)}**"
            f"（{d['rt']['min_at'] if d['rt'] else '—'}）。",
        ]
        if d.get("load"):
            ld = d["load"]
            lines.append(
                f"- **负荷与预测**：本周日均负荷 **{fmt(ld['avg_mw'], 0)} MW**（最高 **{fmt(ld['peak_mw'], 0)} MW**），"
                f"周环比 **{fmt(ld['wow_pct'], 1, True, '%')}**；负荷预测平均偏差 **{fmt(ld['err_mw'], 0, True, ' MW')}**（{fmt(ld['err_pct'], 1, True, '%')}）。"
            )
        if d.get("fuel_mix"):
            fm = d["fuel_mix"]
            top3 = "、".join(f"{k} {v:.1f}%" for k, v in list(fm["share_pct"].items())[:3])
            lines.append(
                f"- **发电结构与净负荷**：主力电源为 {top3}；风光合计占比 **{fmt(fm['ren_share_pct'], 1, suffix='%')}**，"
                f"周内最大3小时净负荷爬坡 **{fmt(fm['max_3h_ramp_mw'], 0)} MW**。"
            )
        if d.get("as_summary"):
            as_txt = "；".join(f"{a['label']} DA {fmt(a['da_avg'], 2)}" + (f" / RT {fmt(a['rt_avg'], 2)}" if a.get("rt_avg") is not None else "") for a in d["as_summary"][:4])
            lines.append(f"- **辅助服务周均价（$/MW·h）**：{as_txt}。")
        lines += ["", f"![{d['label']}周度走势](charts/{iso}_weekly.png)", ""]

    return "\n".join(lines) + "\n"


def build(args):
    D = pd.Timestamp(args.date)
    w_days = day_range(D - timedelta(days=8), 7)      # Sun .. Sat
    p_days = day_range(D - timedelta(days=15), 7)     # Prev Sun .. Sat
    early_days = [ (D - timedelta(days=1)).strftime("%Y-%m-%d"), D.strftime("%Y-%m-%d") ]
    title_range = f"{w_days[0]} ~ {w_days[-1]}"

    out = args.out or os.path.join("reports", "weekly", D.strftime("%Y-%m-%d"))
    os.makedirs(os.path.join(out, "charts"), exist_ok=True)
    family = setup_fonts()

    pr = load_window(args.data, D.strftime("%Y-%m-%d"), "prices.csv", ["iso", "market", "location", "interval_start_utc"])
    asp = load_window(args.data, D.strftime("%Y-%m-%d"), "as_prices.csv", ["iso", "market", "product", "zone", "interval_start_utc"])
    ld = load_window(args.data, D.strftime("%Y-%m-%d"), "load.csv", ["iso", "kind", "interval_start_utc"])
    fm = load_window(args.data, D.strftime("%Y-%m-%d"), "fuel_mix.csv", ["iso", "fuel", "interval_start_utc"])
    gas = load_window(args.data, D.strftime("%Y-%m-%d"), "gas_prices.csv", ["source", "hub", "day"], has_he=False)

    if len(fm):
        fm["group"] = fm["fuel"].map(fuel_group)
    if len(asp):
        lab = asp["product"].map(as_label)
        asp["label"] = [x[0] for x in lab]
        asp["order"] = [x[1] for x in lab]
        asp = asp[asp["label"].notna()]

    summary = {
        "report_date": D.strftime("%Y-%m-%d"),
        "generated_at_bj": (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
        "chart_font": family,
        "review_week": {"start": w_days[0], "end": w_days[-1], "days": w_days},
        "prev_week": {"start": p_days[0], "end": p_days[-1], "days": p_days},
        "isos": {},
        "overview": [],
        "basis": [],
        "gas_weekly": [],
        "early_week_outlook": [],
        "highlights": [],
    }

    # Gas weekly
    if len(gas):
        for hub in sorted(gas["hub"].unique()):
            cur_g = gas[(gas.hub == hub) & (gas.day.isin(w_days))]["value"].dropna()
            prv_g = gas[(gas.hub == hub) & (gas.day.isin(p_days))]["value"].dropna()
            if len(cur_g) or len(prv_g):
                c_avg = r(cur_g.mean(), 3) if len(cur_g) else None
                p_avg = r(prv_g.mean(), 3) if len(prv_g) else None
                summary["gas_weekly"].append({
                    "hub": hub, "cur": c_avg, "prev": p_avg, "wow_pct": pct(c_avg, p_avg),
                })

    mix_chart_rows = []
    for iso in ISO_ORDER:
        info = ISO_INFO[iso]
        main_hub = HUBS[iso][0]
        pr_iso = pr[pr.iso == iso]
        da_cur = price_block(pr_iso, "DA", main_hub, w_days)
        da_prv = price_block(pr_iso, "DA", main_hub, p_days)
        rt_cur = price_block(pr_iso, "RT", main_hub, w_days)
        rt_prv = price_block(pr_iso, "RT", main_hub, p_days)

        # DART weekly
        da_s = pr_iso[(pr_iso.market == "DA") & (pr_iso.location == main_hub) & (pr_iso.day.isin(w_days))].set_index("ts")["value"]
        rt_s = pr_iso[(pr_iso.market == "RT") & (pr_iso.location == main_hub) & (pr_iso.day.isin(w_days))].set_index("ts")["value"]
        both = pd.concat([da_s, rt_s], axis=1, keys=["da", "rt"]).dropna()
        dart_avg = r((both["da"] - both["rt"]).mean()) if len(both) else None

        # Basis pair (RT)
        h_a, h_b, pair_lbl = BASIS_PAIRS[iso]
        def _basis(days):
            sa = pr_iso[(pr_iso.market == "RT") & (pr_iso.location == h_a) & (pr_iso.day.isin(days))].set_index("ts")["value"]
            sb = pr_iso[(pr_iso.market == "RT") & (pr_iso.location == h_b) & (pr_iso.day.isin(days))].set_index("ts")["value"]
            m = pd.concat([sa, sb], axis=1, keys=["a", "b"]).dropna()
            return r((m["a"] - m["b"]).mean()) if len(m) else None
        summary["basis"].append({
            "iso": iso, "iso_label": info["label"], "pair": pair_lbl,
            "cur_avg": _basis(w_days), "prev_avg": _basis(p_days),
        })

        # Load weekly
        ld_iso = ld[ld.iso == iso]
        ac_cur = ld_iso[(ld_iso.kind == "actual") & (ld_iso.day.isin(w_days))]
        ac_prv = ld_iso[(ld_iso.kind == "actual") & (ld_iso.day.isin(p_days))]
        fc_cur = ld_iso[(ld_iso.kind == "forecast") & (ld_iso.day.isin(w_days))]
        ld_avg = r(ac_cur["value"].mean(), 0) if len(ac_cur) else None
        ld_prv_avg = r(ac_prv["value"].mean(), 0) if len(ac_prv) else None
        ld_peak = r(ac_cur["value"].max(), 0) if len(ac_cur) else None
        err_mw, err_pct = None, None
        if len(ac_cur) and len(fc_cur):
            m_ld = pd.concat([ac_cur.set_index("ts")["value"], fc_cur.set_index("ts")["value"]],
                             axis=1, keys=["act", "fc"]).dropna()
            if len(m_ld):
                diff = m_ld["act"] - m_ld["fc"]
                err_mw = r(diff.mean(), 0)
                err_pct = r(diff.mean() / m_ld["fc"].mean() * 100, 1) if m_ld["fc"].mean() else None

        load_block = {
            "avg_mw": ld_avg, "peak_mw": ld_peak, "wow_pct": pct(ld_avg, ld_prv_avg),
            "err_mw": err_mw, "err_pct": err_pct,
        }

        # Fuel Mix weekly
        fm_iso = fm[(fm.iso == iso) & (fm.day.isin(w_days))]
        mix_block = None
        if not fm_iso.empty:
            by_grp = fm_iso.groupby("group")["value"].mean()
            pos_total = by_grp[by_grp > 0].sum()
            shares = {k: r(v / pos_total * 100, 1) for k, v in by_grp.sort_values(ascending=False).items() if v > 0 and pos_total > 0}
            ren_pct = r((by_grp.get("风电", 0) + by_grp.get("光伏", 0)) / pos_total * 100, 1) if pos_total > 0 else None
            ren_ts = fm_iso[fm_iso.group.isin(["风电", "光伏"])].groupby("ts")["value"].sum()
            max_ramp = None
            if len(ac_cur) and len(ren_ts):
                nl = ac_cur.set_index("ts")["value"].sub(ren_ts, fill_value=0).sort_index().dropna()
                if len(nl) >= 4:
                    max_ramp = r(nl.diff(3).max(), 0)
            mix_block = {"share_pct": shares, "ren_share_pct": ren_pct, "max_3h_ramp_mw": max_ramp}
        mix_chart_rows.append({"iso": iso, "label": info["label"], "share_pct": mix_block["share_pct"] if mix_block else {}})

        # AS summary
        asp_iso = asp[(asp.iso == iso) & (asp.day.isin(w_days))]
        as_list = []
        if not asp_iso.empty:
            for lab in asp_iso.groupby("label")["order"].min().sort_values().index:
                da_v = asp_iso[(asp_iso.label == lab) & (asp_iso.market == "DA")]["value"].mean()
                rt_v = asp_iso[(asp_iso.label == lab) & (asp_iso.market == "RT")]["value"].mean()
                as_list.append({
                    "label": lab,
                    "da_avg": r(da_v) if pd.notna(da_v) else None,
                    "rt_avg": r(rt_v) if pd.notna(rt_v) else None,
                })

        # Early week DA outlook (Sun & Mon)
        sun_da = pr_iso[(pr_iso.market == "DA") & (pr_iso.location == main_hub) & (pr_iso.day == early_days[0])]["value"].mean()
        mon_da = pr_iso[(pr_iso.market == "DA") & (pr_iso.location == main_hub) & (pr_iso.day == early_days[1])]["value"].mean()
        mon_ld = ld_iso[(ld_iso.kind == "forecast") & (ld_iso.day == early_days[1])]["value"].max()
        sun_da_v = r(sun_da) if pd.notna(sun_da) else None
        mon_da_v = r(mon_da) if pd.notna(mon_da) else None
        summary["early_week_outlook"].append({
            "iso": iso, "label": info["label"], "main_hub": HUB_LABELS.get(main_hub, main_hub),
            "sun_da_avg": sun_da_v, "mon_da_avg": mon_da_v,
            "mon_vs_last_da_pct": pct(mon_da_v, da_cur["avg"] if da_cur else None),
            "mon_load_peak_mw": r(mon_ld, 0) if pd.notna(mon_ld) else None,
        })

        rt_wow = pct(rt_cur["avg"] if rt_cur else None, rt_prv["avg"] if rt_prv else None)
        summary["isos"][iso] = {
            "label": info["label"], "name_cn": info["cn"],
            "main_hub": HUB_LABELS.get(main_hub, main_hub),
            "da": da_cur, "da_prev": da_prv, "rt": rt_cur, "rt_prev": rt_prv,
            "rt_wow_pct": rt_wow, "dart_avg": dart_avg,
            "load": load_block, "fuel_mix": mix_block, "as_summary": as_list,
        }
        summary["overview"].append({
            "iso": iso, "label": info["label"], "main_hub": HUB_LABELS.get(main_hub, main_hub),
            "rt_avg": rt_cur["avg"] if rt_cur else None,
            "rt_prev_avg": rt_prv["avg"] if rt_prv else None,
            "rt_wow_pct": rt_wow,
            "rt_peak_avg": rt_cur["peak_avg"] if rt_cur else None,
            "rt_offpeak_avg": rt_cur["offpeak_avg"] if rt_cur else None,
            "rt_neg_hours": rt_cur["neg_hours"] if rt_cur else None,
            "da_avg": da_cur["avg"] if da_cur else None,
            "dart_avg": dart_avg,
            "load_avg_mw": ld_avg, "load_wow_pct": load_block["wow_pct"],
            "load_err_pct": err_pct,
            "ren_share_pct": mix_block["ren_share_pct"] if mix_block else None,
        })

        chart_iso_weekly(
            iso, pr_iso[pr_iso.day.isin(w_days)], fm_iso,
            ld_iso[ld_iso.day.isin(w_days)], main_hub,
            os.path.join(out, "charts", f"{iso}_weekly.png"),
        )

    # Auto highlights
    valid_rt = [o for o in summary["overview"] if o["rt_avg"] is not None]
    if valid_rt:
        hi = max(valid_rt, key=lambda x: x["rt_avg"])
        lo = min(valid_rt, key=lambda x: x["rt_avg"])
        summary["highlights"].append(
            f"**价格格局**：本周全美最高 RT 均价出现在 **{hi['label']}**（{hi['rt_avg']:.1f} $/MWh），最低为 **{lo['label']}**（{lo['rt_avg']:.1f} $/MWh）。"
        )
        movers = [o for o in valid_rt if o["rt_wow_pct"] is not None]
        if movers:
            top_m = max(movers, key=lambda x: abs(x["rt_wow_pct"]))
            summary["highlights"].append(
                f"**最大波动区**：**{top_m['label']}** 实时均价环比变动 **{top_m['rt_wow_pct']:+.1f}%**。"
            )
    neg_isos = [f"{o['label']}({o['rt_neg_hours']}h)" for o in valid_rt if (o["rt_neg_hours"] or 0) > 0]
    if neg_isos:
        summary["highlights"].append(f"**负电价频次**：本周出现实时负电价的地区包括 {'、'.join(neg_isos)}。")
    dart_outliers = [o for o in summary["overview"] if o["dart_avg"] is not None and abs(o["dart_avg"]) >= 5]
    if dart_outliers:
        txt = "、".join(f"{o['label']}({o['dart_avg']:+.1f})" for o in dart_outliers)
        summary["highlights"].append(f"**DART 系统性偏差**：本周日均 |DA-RT| ≥ 5 $/MWh 的市场：{txt}。")

    chart_weekly_bar(summary["overview"], title_range, os.path.join(out, "charts", "overview_weekly_bar.png"))
    chart_dart_weekly(summary["overview"], title_range, os.path.join(out, "charts", "overview_dart_weekly.png"))
    chart_basis(summary["basis"], title_range, os.path.join(out, "charts", "overview_basis.png"))
    chart_mix_share(mix_chart_rows, title_range, os.path.join(out, "charts", "overview_mix_share.png"))

    with open(os.path.join(out, "weekly_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    md = render_markdown(summary)
    with open(os.path.join(out, "weekly_report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(f"weekly report written to {out} | font={family} | review_week={title_range}")


def default_monday_date() -> str:
    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    return now_bj.strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--date", default=default_monday_date(), help="Monday report date D (YYYY-MM-DD, Beijing)")
    ap.add_argument("--out", default="", help="Output directory (default: reports/weekly/<D>)")
    args = ap.parse_args()
    build(args)


if __name__ == "__main__":
    main()