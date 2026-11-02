#!/usr/bin/env node
/*
 * Build the Word report from work/summary.json + work/commentary.json + work/charts/*.png
 *
 * Usage: node report/build_docx.js <workdir> <output.docx>
 *
 * commentary.json:
 *   { "headline": ["要点1", ...], "iso": {"CAISO": "点评...", ...}, "notes": "可选" }
 */
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, ImageRun, Table, TableRow, TableCell,
  WidthType, AlignmentType, BorderStyle, ShadingType, Header, Footer, PageNumber, LevelFormat,
  TableLayoutType, VerticalAlign, PageBreak, LineRuleType,
} = require("docx");

const [workDir, outFile] = process.argv.slice(2);
if (!workDir || !outFile) {
  console.error("usage: node build_docx.js <workdir> <output.docx>");
  process.exit(2);
}
const S = JSON.parse(fs.readFileSync(path.join(workDir, "summary.json"), "utf8"));
const cPath = path.join(workDir, "commentary.json");
const C = fs.existsSync(cPath) ? JSON.parse(fs.readFileSync(cPath, "utf8")) : { headline: [], iso: {} };

// ---------------------------------------------------------------- layout
const PAGE_W = 11906, PAGE_H = 16838, MARGIN = 1134;           // A4, 2 cm margins
const CONTENT_W = PAGE_W - 2 * MARGIN;                         // 9638 DXA
const PX_PER_DXA = 96 / 1440;
const FONT = { ascii: "Arial", hAnsi: "Arial", eastAsia: "Microsoft YaHei", cs: "Arial" };
const INK = "1B1B1A", INK2 = "52514E", MUTED = "8A8984", RULE = "D9D8D2", HEAD_FILL = "F1F0EC";
const ISO_ORDER = ["CAISO", "ERCOT", "PJM", "MISO", "NYISO", "ISONE", "SPP"];

let figNo = 0, tabNo = 0;

// ---------------------------------------------------------------- formatting
const isNum = (v) => typeof v === "number" && isFinite(v);
const f1 = (v) => (isNum(v) ? v.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "—");
const f2 = (v) => (isNum(v) ? v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—");
const f0 = (v) => (isNum(v) ? Math.round(v).toLocaleString("en-US") : "—");
const sgn = (v, f = f1) => (isNum(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + f(Math.abs(v)) : "—");
const sgnPct = (v) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}%` : "—");
const gw = (v) => (isNum(v) ? (v / 1000).toFixed(1) : "—");
const withHe = (v, he, f = f1) => (isNum(v) ? `${f(v)} (HE${he})` : "—");

// ---------------------------------------------------------------- primitives
function run(text, o = {}) {
  return new TextRun({ text: String(text), font: FONT, size: o.size || 21, bold: !!o.bold,
    color: o.color || INK, italics: !!o.italics });
}
function para(children, o = {}) {
  if (typeof children === "string") children = [run(children, o)];
  const spacing = { before: o.before ?? 0, after: o.after ?? 120 };
  if (!o.image) { spacing.line = o.line || 300; spacing.lineRule = LineRuleType.AUTO; }
  return new Paragraph({ children, alignment: o.align || AlignmentType.LEFT, spacing,
    keepNext: !!o.keepNext, numbering: o.numbering });
}
function h1(text, o = {}) {
  return new Paragraph({ heading: HeadingLevel.HEADING_1, children: [run(text, { size: 30, bold: true })],
    spacing: { before: o.pageBreak ? 0 : 240, after: 120 }, keepNext: true, pageBreakBefore: !!o.pageBreak });
}
function h2(text) {
  return new Paragraph({ heading: HeadingLevel.HEADING_2, children: [run(text, { size: 26, bold: true })],
    spacing: { before: 240, after: 100 }, keepNext: true });
}
function caption(text) {
  return para([run(text, { size: 17, color: INK2 })], { align: AlignmentType.CENTER, after: 160 });
}
function muted(text, o = {}) {
  return para([run(text, { size: 17, color: MUTED })], { after: o.after ?? 100 });
}
function pngSize(file) {
  const b = fs.readFileSync(file);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20), data: b };
}
function image(relPath, widthDxa) {
  const file = path.join(workDir, relPath);
  if (!fs.existsSync(file)) return run("（图表缺失）", { color: MUTED, size: 18 });
  const { w, h, data } = pngSize(file);
  const width = Math.round(widthDxa * PX_PER_DXA);
  return new ImageRun({ type: "png", data, transformation: { width, height: Math.round(width * h / w) } });
}
function figure(relPath, title, widthDxa = CONTENT_W) {
  figNo += 1;
  return [
    para([image(relPath, widthDxa)], { align: AlignmentType.CENTER, after: 40, keepNext: true, image: true }),
    caption(`图${figNo}　${title}`),
  ];
}

const border = { style: BorderStyle.SINGLE, size: 4, color: RULE };
const BORDERS = { top: border, bottom: border, left: border, right: border, insideHorizontal: border, insideVertical: border };
const NO_BORDER = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const NO_BORDERS = { top: NO_BORDER, bottom: NO_BORDER, left: NO_BORDER, right: NO_BORDER, insideHorizontal: NO_BORDER, insideVertical: NO_BORDER };

function cell(text, width, o = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: o.fill ? { type: ShadingType.CLEAR, color: "auto", fill: o.fill } : undefined,
    verticalAlign: VerticalAlign.CENTER,
    margins: { top: 30, bottom: 30, left: 60, right: 60 },
    children: String(text).split("\n").map((line) => new Paragraph({ alignment: o.align || AlignmentType.LEFT,
      spacing: { before: 0, after: 0, line: 260, lineRule: LineRuleType.AUTO },
      children: [run(line, { size: o.size || 17, bold: !!o.bold, color: o.color || INK })] })),
  });
}
/** cols: [{title, width(weight), align}] ; rows: array of arrays of strings */
function tableBlock(title, cols, rows, totalW = CONTENT_W, o = {}) {
  tabNo += 1;
  const total = cols.reduce((a, c) => a + c.width, 0);
  const widths = cols.map((c) => Math.floor((c.width / total) * totalW));
  widths[widths.length - 1] += totalW - widths.reduce((a, b) => a + b, 0);
  const header = new TableRow({ tableHeader: true, cantSplit: true,
    children: cols.map((c, i) => cell(c.title, widths[i], { bold: true, fill: HEAD_FILL,
      align: i === 0 ? AlignmentType.LEFT : AlignmentType.CENTER, size: 16 })) });
  const body = rows.map((r) => new TableRow({ cantSplit: true,
    children: r.map((v, i) => cell(v, widths[i], { align: cols[i].align || (i === 0 ? AlignmentType.LEFT : AlignmentType.RIGHT) })) }));
  return [
    para([run(`表${tabNo}　${title}`, { size: 18, bold: true, color: INK2 })], { after: 60, keepNext: true }),
    new Table({ width: { size: totalW, type: WidthType.DXA }, columnWidths: widths,
      layout: TableLayoutType.FIXED, borders: BORDERS, rows: [header, ...body] }),
  ];
}
function table(title, cols, rows, o = {}) {
  return [...tableBlock(title, cols, rows, CONTENT_W, o), para("", { after: o.after ?? 120, line: 200 })];
}
/** two blocks (arrays of Paragraph/Table) next to each other, borderless */
function sideBySideBlocks(left, right, gap = 240) {
  const half = Math.floor((CONTENT_W - gap) / 2);
  const mk = (kids, w, pad) => new TableCell({ width: { size: w, type: WidthType.DXA }, borders: NO_BORDERS,
    margins: { top: 0, bottom: 0, left: 0, right: pad }, children: kids.length ? kids : [para("")] });
  return [new Table({ width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: [half + gap, CONTENT_W - half - gap],
    layout: TableLayoutType.FIXED, borders: NO_BORDERS,
    rows: [new TableRow({ cantSplit: true, children: [mk(left, half + gap, gap), mk(right, CONTENT_W - half - gap, 0)] })] }),
    para("", { after: 100, line: 200 })];
}
const HALF_W = Math.floor((CONTENT_W - 240) / 2);
function sideBySide(left, right) {
  const half = Math.floor(CONTENT_W / 2);
  const mk = (rel, w) => new TableCell({ width: { size: w, type: WidthType.DXA }, borders: NO_BORDERS,
    margins: { top: 0, bottom: 0, left: 40, right: 40 },
    children: [para([image(rel, w - 120)], { align: AlignmentType.CENTER, after: 0, image: true })] });
  return new Table({ width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: [half, CONTENT_W - half],
    layout: TableLayoutType.FIXED, borders: NO_BORDERS,
    rows: [new TableRow({ cantSplit: true, children: [mk(left, half), mk(right, CONTENT_W - half)] })] });
}
function bullets(items) {
  return items.map((t) => new Paragraph({ numbering: { reference: "bullets", level: 0 },
    spacing: { before: 0, after: 80, line: 300, lineRule: LineRuleType.AUTO }, children: [run(t)] }));
}

// ---------------------------------------------------------------- content
const children = [];
const D = S.da_day, D1 = S.da_prev_day, D2 = S.rt_day;

children.push(new Paragraph({ alignment: AlignmentType.LEFT, spacing: { before: 0, after: 80 },
  children: [run("美国各ISO电力市场日报", { size: 40, bold: true })] }));
children.push(para([run(`日前运行日 ${D}　｜　实时运行日 ${D2}　｜　生成时间 北京时间 ${S.generated_at_bj}`, { size: 19, color: INK2 })],
  { after: 40 }));
children.push(muted("覆盖 CAISO、ERCOT、PJM、MISO、NYISO、ISO-NE、SPP。运行日均为各ISO当地日期；HE 为小时结束编号（HE1 = 0:00–1:00）。" +
  "能量价格单位 $/MWh，辅助服务价格单位 $/MW·h。", { after: 100 }));

// 1. headline
children.push(h1("一、要点概览"));
const headline = (C.headline && C.headline.length) ? C.headline : ["（本期未生成点评）"];
children.push(...bullets(headline));

// 2. overview
children.push(h1("二、市场总览"));
const ovRows = S.overview.map((o) => [
  o.label, o.main_hub, f2(o.da_avg), sgn(o.da_chg, f2), withHe(o.da_max, S.isos[o.iso]?.hubs?.[0]?.da?.max_he ?? o.da_max_he),
  f2(o.rt_avg), sgn(o.da_minus_rt, f2), gw(o.load_fc_peak), gw(o.load_act_peak),
]);
children.push(...table(`各ISO核心指标（主枢纽；日前 ${D}，实时 ${D2}）`, [
  { title: "ISO", width: 8 }, { title: "主枢纽", width: 13 }, { title: `日前均价\n${D.slice(5)}`, width: 10 },
  { title: "较前日", width: 9 }, { title: "日前最高价\n(时段)", width: 15 }, { title: `实时均价\n${D2.slice(5)}`, width: 10 },
  { title: `日前−实时\n${D2.slice(5)}`, width: 10 }, { title: `预测峰荷 GW\n${D.slice(5)}`, width: 11 },
  { title: `实际峰荷 GW\n${D2.slice(5)}`, width: 11 },
], ovRows));
children.push(...figure(S.charts.overview_profiles, `各ISO主枢纽日前逐时价格（${D}）`));

// 3. per ISO
children.push(h1("三、分区域详情", { pageBreak: true }));
ISO_ORDER.forEach((iso, idx) => {
  const X = S.isos[iso];
  if (!X) return;
  children.push(new Paragraph({ heading: HeadingLevel.HEADING_2, keepNext: true, pageBreakBefore: idx > 0,
    spacing: { before: idx > 0 ? 0 : 120, after: 100 },
    children: [run(`${idx + 1}. ${X.label}｜${X.name_cn}`, { size: 26, bold: true }),
      run(`　　${X.tz_cn} · 主枢纽 ${X.main_hub}`, { size: 17, color: MUTED })] }));
  const comment = (C.iso && C.iso[iso]) || "（本期未生成点评）";
  children.push(para([run("点评：", { bold: true }), run(comment)], { after: 140 }));

  children.push(...figure(X.charts.price, `${X.label} 日前（${D}）与实时（${D2}）出清价格`));
  children.push(sideBySide(X.charts.load, X.charts.as));
  figNo += 1;
  const asTitle = X.as ? (X.as.market === "DA" ? "日前辅助服务出清价" : "实时备用出清价") : "辅助服务出清价";
  children.push(caption(`图${figNo}　${X.label} 负荷（左）与${asTitle}（右）`));

  // price table
  const pRows = (X.hubs || []).map((h) => [
    h.label, f2(h.da?.avg), sgn(h.da_chg, f2), f2(h.da?.peak_avg),
    withHe(h.da?.max, h.da?.max_he), withHe(h.da?.min, h.da?.min_he),
    f2(h.rt?.avg), withHe(h.rt?.max, h.rt?.max_he), sgn(h.da_minus_rt, f2),
  ]);
  if (pRows.length) {
    children.push(...table(`${X.label} 价格统计（$/MWh；日前 ${D}，实时 ${D2}）`, [
      { title: "节点/枢纽", width: 14 }, { title: "日前均价", width: 9 }, { title: "较前日", width: 8 },
      { title: "峰段均价\nHE8–23", width: 9 }, { title: "日前最高\n(时段)", width: 14 }, { title: "日前最低\n(时段)", width: 14 },
      { title: "实时均价", width: 9 }, { title: "实时最高\n(时段)", width: 14 }, { title: "日前−\n实时", width: 10 },
    ], pRows, { after: 120 }));
  } else {
    children.push(muted("价格数据暂缺。"));
  }

  // AS table (left) + load table (right)
  let leftBlk = [], rightBlk = [];
  if (X.as && X.as.products && X.as.products.length) {
    const asRows = X.as.products.map((p) => [p.label, f2(p.avg), X.as.market === "DA" ? sgn(p.chg, f2) : "—", withHe(p.max, p.max_he, f2)]);
    const zone = X.as.zone && X.as.zone !== "System" ? `，${X.as.zone}` : "";
    leftBlk = tableBlock(`${X.label} ${asTitle}（$/MW·h；${X.as.day.slice(5)}${zone}）`, [
      { title: "品种", width: 30 }, { title: "日均价", width: 17 }, { title: "较前日", width: 17 }, { title: "最高(时段)", width: 26 },
    ], asRows, HALF_W);
  }
  const L = X.load || {};
  const lRows = [];
  if (L.forecast) lRows.push([`${L.forecast.day.slice(5)} 预测`, withHe(L.forecast.peak, L.forecast.peak_he, f0), f0(L.forecast.avg), withHe(L.forecast.min, L.forecast.min_he, f0)]);
  if (L.actual) lRows.push([`${L.actual.day.slice(5)} 实际`, withHe(L.actual.peak, L.actual.peak_he, f0), f0(L.actual.avg), withHe(L.actual.min, L.actual.min_he, f0)]);
  if (lRows.length) {
    rightBlk = tableBlock(`${X.label} 系统负荷（MW）`, [
      { title: "项目", width: 20 }, { title: "峰值(时段)", width: 28 }, { title: "日均", width: 16 }, { title: "谷值(时段)", width: 28 },
    ], lRows, HALF_W);
  }
  if (leftBlk.length || rightBlk.length) children.push(...sideBySideBlocks(leftBlk, rightBlk).slice(0, 1));
  if (X.failed_datasets && X.failed_datasets.length) {
    children.push(muted(`数据缺失：${X.failed_datasets.join("、")}（详见第四部分）`));
  }
});

// 4. notes
children.push(h1("四、数据说明", { pageBreak: true }));
const notes = [
  "数据来源：各ISO官方公开数据，通过开源库 gridstatus 采集；采集任务由 GitHub Actions 每日北京时间 06:47 运行（08:17 重试失败项）。",
  `日前价格为日前市场节点边际电价（ERCOT 为结算点价格 SPP），本期运行日 ${D}；“较前日”对比 ${D1}。`,
  `实时价格为 ${D2} 实时市场价格的小时均值：CAISO 为 5 分钟 RTD，ERCOT 为 15 分钟 SPP，SPP 为 5 分钟 LMP，PJM、MISO、NYISO、ISO-NE 为官方小时价格（MISO 为初步值，PJM 视发布情况为已核实或未核实值）。` +
  "“日前−实时”为同一运行日日前均价减实时均价，正值表示日前溢价。",
  "峰段均价按 HE8–HE23 计算，未区分工作日与节假日。",
  "辅助服务：CAISO 为全系统扩展区域日前价；ERCOT 为日前 MCPC；PJM 为 RTO 备用区日前价；MISO 为日前事前出清价（MISO Wide 全网值，发电资源口径）；NYISO 为 Zone J（纽约市）日前价；SPP 为各备用区日前价的平均；ISO-NE 取实时备用价格（ROS 备用区，5 分钟最终值的小时均值；TMSR 为 10 分钟旋转备用，TMR 为 10 分钟备用，总备用含 30 分钟备用）。",
  `负荷：预测为采集时点最新一版 ${D} 系统负荷预测；实际为 ${D2} 系统负荷的小时均值。`,
];
children.push(...bullets(notes));
const issues = S.data_issues || [];
if (issues.length) {
  children.push(para([run("本期数据缺失或异常：", { bold: true })], { before: 120, after: 60 }));
  children.push(...bullets(issues));
}
if (C.notes) children.push(para([run("补充说明：", { bold: true }), run(C.notes)], { before: 120 }));

// ---------------------------------------------------------------- document
const doc = new Document({
  creator: "Claude",
  title: `美国各ISO电力市场日报 ${D}`,
  styles: {
    default: { document: { run: { font: FONT, size: 21, color: INK } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 30, bold: true, color: INK }, paragraph: { spacing: { before: 280, after: 140 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 26, bold: true, color: INK }, paragraph: { spacing: { before: 240, after: 100 }, outlineLevel: 1 } },
    ],
  },
  numbering: { config: [{ reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•",
    alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 420, hanging: 280 } } } }] }] },
  sections: [{
    properties: { page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN, header: 560, footer: 560 } } },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT,
      children: [run(`美国各ISO电力市场日报｜${D}`, { size: 16, color: MUTED })] })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
      children: [new TextRun({ children: ["第 ", PageNumber.CURRENT, " 页 / 共 ", PageNumber.TOTAL_PAGES, " 页"], font: FONT, size: 16, color: MUTED })] })] }) },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(outFile, buf);
  console.log(`wrote ${outFile} (${(buf.length / 1024).toFixed(0)} KB, ${figNo} figures, ${tabNo} tables)`);
});
