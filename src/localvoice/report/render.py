"""Render a measurement run as Markdown and as a self-contained HTML page.

Markdown is what goes in a pull request or a README. HTML is what you open when
you want to see the shape of the distribution rather than four percentiles of it.
The HTML has no build step, no CDN, and no network access at view time: a single
file you can attach to an issue and expect someone to actually open.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path

from localvoice.probe.runner import RunResult
from localvoice.report.stats import (
    BudgetSlice,
    Distribution,
    TurnStats,
    budget,
    representative_turn,
    summarize,
)

# Categorical slots 1-4 from the validated reference palette, in fixed order.
# Stage colors are assigned by position in the pipeline and never cycled, so
# "the orange one" means ASR in every chart in this repository.
SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
SERIES_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500")


@dataclass
class Report:
    markdown: str
    html: str

    def write(self, directory: Path, stem: str = "report") -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        markdown_path = directory / f"{stem}.md"
        html_path = directory / f"{stem}.html"
        markdown_path.write_text(self.markdown, encoding="utf-8")
        html_path.write_text(self.html, encoding="utf-8")
        return markdown_path, html_path


def _fmt(value: float | None, unit: str = "ms") -> str:
    if value is None:
        return "—"
    if unit == "x":
        return f"{value:.2f}x"
    if value >= 1000:
        return f"{value / 1000:.2f} s"
    return f"{value:.0f} ms"


def _distribution_rows(distributions: list[Distribution]) -> str:
    rows = []
    for d in distributions:
        rows.append(
            "| {label} | {n} | {p50} | {p90} | {p95} | {p99} | {max} |".format(
                label=d.label,
                n=d.count,
                p50=_fmt(d.percentiles[50], d.unit),
                p90=_fmt(d.percentiles[90], d.unit),
                p95=_fmt(d.percentiles[95], d.unit),
                p99=_fmt(d.percentiles[99], d.unit),
                max=_fmt(d.maximum, d.unit),
            )
        )
    return "\n".join(rows)


def render_markdown(result: RunResult, stats: TurnStats, slices: list[BudgetSlice]) -> str:
    spec = result.spec
    lines: list[str] = []
    lines.append(f"# {spec.name}")
    lines.append("")
    lines.append(
        f"{spec.concurrency} concurrent session(s), {spec.turns} turn(s) each, "
        f"{spec.warmup_turns} warmup turn(s) excluded. Started {result.started_at}."
    )
    if spec.notes:
        lines.append("")
        lines.append(spec.notes)
    if spec.faults:
        lines.append("")
        lines.append(f"**Injected faults:** `{'`, `'.join(spec.faults)}`")
    lines.append("")

    gpus = result.environment.get("gpus")
    lines.append(f"Host `{result.environment.get('host', '?')}`" + (f", {'; '.join(gpus)}" if gpus else ""))
    lines.append("")

    lines.append("## Outcome")
    lines.append("")
    lines.append("| Measure | Value |")
    lines.append("|---|---|")
    lines.append(f"| Turns measured | {stats.total_turns} |")
    lines.append(f"| Completed | {stats.completed_turns} ({stats.completion_rate:.1%}) |")
    lines.append(f"| Cancelled | {stats.cancelled_turns} |")
    lines.append(f"| Failed | {stats.failed_turns} |")
    lines.append(f"| Timed out | {stats.timed_out_turns} |")
    lines.append(f"| Sessions refused | {result.rejected_sessions} / {spec.concurrency} |")
    lines.append(f"| Wall time | {result.duration_s:.1f} s |")
    lines.append("")

    if stats.headline:
        lines.append("## Headline latency")
        lines.append("")
        lines.append("| Measure | n | p50 | p90 | p95 | p99 | max |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        lines.append(_distribution_rows(stats.headline))
        lines.append("")

    if stats.stages:
        lines.append("## Stage breakdown")
        lines.append("")
        lines.append("Per-stage percentiles are computed independently, so they are not")
        lines.append("expected to sum to the headline total — the slowest ASR turn and the")
        lines.append("slowest TTS turn are rarely the same turn.")
        lines.append("")
        lines.append("| Stage | n | p50 | p90 | p95 | p99 | max |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        lines.append(_distribution_rows(stats.stages))
        lines.append("")

    if slices:
        total = sum(s.ms for s in slices)
        lines.append("## Where one median turn spends its time")
        lines.append("")
        lines.append("A single representative turn, so the parts genuinely add up.")
        lines.append("")
        lines.append("| Stage | Time | Share |")
        lines.append("|---|---:|---:|")
        for item in slices:
            lines.append(f"| {item.label} | {_fmt(item.ms)} | {item.share:.0%} |")
        lines.append(f"| **Total** | **{_fmt(total)}** | |")
        lines.append("")

    if stats.realtime_factor is not None:
        rtf = stats.realtime_factor
        lines.append("## Realtime factor")
        lines.append("")
        lines.append(
            f"Median {rtf.p50:.2f}x, worst {rtf.minimum:.2f}x. "
            "Above 1.0x means speech is synthesized faster than it is spoken, which is "
            "the condition for a conversation to continue without the audio running dry."
        )
        lines.append("")

    if stats.error_counts:
        lines.append("## Errors")
        lines.append("")
        lines.append("| Error | Count |")
        lines.append("|---|---:|")
        for name, count in sorted(stats.error_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{name}` | {count} |")
        lines.append("")

    return "\n".join(lines) + "\n"


def render_html(result: RunResult, stats: TurnStats, slices: list[BudgetSlice]) -> str:
    payload = {
        "spec": result.to_json()["spec"],
        "environment": result.environment,
        "stats": stats.to_json(),
        "budget": [{"name": s.name, "label": s.label, "ms": s.ms, "share": s.share} for s in slices],
        "series_light": list(SERIES_LIGHT),
        "series_dark": list(SERIES_DARK),
        "turns": [turn.to_json() for turn in result.measured_turns],
    }
    data = json.dumps(payload, indent=None, separators=(",", ":"))
    title = html.escape(f"{result.spec.name} — local-realtime-voice")
    return _HTML_TEMPLATE.replace("__TITLE__", title).replace("__DATA__", data)


def render(result: RunResult) -> Report:
    turns = result.measured_turns
    stats = summarize(turns)
    representative = representative_turn(turns)
    slices = budget(representative) if representative is not None else []
    return Report(
        markdown=render_markdown(result, stats, slices),
        html=render_html(result, stats, slices),
    )


# The template is one file on purpose: no CDN, no build, opens offline.
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  .viz-root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --page: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --grid: #e1e0d9;
    --baseline: #c3c2b7;
    --series-1: #2a78d6;
    --series-2: #eb6834;
    --series-3: #1baf7a;
    --series-4: #eda100;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark;
      --surface-1: #1a1a19;
      --page: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --grid: #2c2c2a;
      --baseline: #383835;
      --series-1: #3987e5;
      --series-2: #d95926;
      --series-3: #199e70;
      --series-4: #c98500;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 980px; margin: 0 auto; padding: 40px 24px 80px; }
  h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.01em; }
  h2 { font-size: 17px; margin: 40px 0 6px; letter-spacing: -0.005em; }
  .sub { color: var(--text-secondary); margin: 0 0 8px; font-size: 14px; }
  .note { color: var(--text-muted); font-size: 13px; margin: 0 0 16px; max-width: 62ch; }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--grid);
    border-radius: 10px;
    padding: 20px 22px;
    margin-top: 12px;
  }
  .tiles { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 14px; }
  .tile {
    background: var(--surface-1);
    border: 1px solid var(--grid);
    border-radius: 10px;
    padding: 14px 18px;
    min-width: 150px;
    flex: 1 1 150px;
  }
  .tile .k { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }
  .tile .v { font-size: 26px; font-variant-numeric: tabular-nums; margin-top: 4px; letter-spacing: -0.02em; }
  .tile .d { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--grid); }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--text-muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
  td { font-variant-numeric: tabular-nums; }
  .legend { display: flex; flex-wrap: wrap; gap: 16px; margin: 4px 0 14px; font-size: 13px; color: var(--text-secondary); }
  .legend span { display: inline-flex; align-items: center; gap: 7px; }
  .swatch { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }
  .tip {
    position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--baseline); border-radius: 7px;
    padding: 7px 10px; font-size: 12.5px; box-shadow: 0 4px 14px rgba(0,0,0,.14);
    font-variant-numeric: tabular-nums; z-index: 40; max-width: 260px;
  }
  svg text { font: 12px ui-sans-serif, system-ui, sans-serif; }
  .axis { fill: var(--text-muted); }
  .mark { cursor: default; }
  details { margin-top: 12px; }
  summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
</style>
</head>
<body>
<div class="viz-root wrap">
  <h1 id="title"></h1>
  <p class="sub" id="subtitle"></p>
  <div class="tiles" id="tiles"></div>

  <h2>Where one median turn spends its time</h2>
  <p class="note">A single representative turn, chosen as the completed turn closest to the
  median user-perceived latency, so the stages genuinely add up to the total.</p>
  <div class="legend" id="budget-legend"></div>
  <div class="card"><div id="budget"></div></div>
  <details>
    <summary>Show the same figures as a table</summary>
    <div class="card"><table id="budget-table"></table></div>
  </details>

  <h2>Stage latency percentiles</h2>
  <p class="note">Each stage is summarized independently over every measured turn, so these
  do not sum to the headline total. Nearest-rank percentiles: every value shown is a
  latency that actually occurred.</p>
  <div class="card"><div id="stages"></div></div>

  <h2>Per-turn perceived latency</h2>
  <p class="note">Every measured turn in run order. Look for drift, which means the
  deployment is falling behind, and for isolated spikes, which usually mean a stall
  rather than a slowdown.</p>
  <div class="card"><div id="turns"></div></div>

  <h2>All measures</h2>
  <div class="card"><table id="table"></table></div>
</div>
<div class="tip" id="tip"></div>
<script id="payload" type="application/json">__DATA__</script>
<script>
(function () {
  const D = JSON.parse(document.getElementById("payload").textContent);
  const tip = document.getElementById("tip");
  const NS = "http://www.w3.org/2000/svg";
  const css = (name) => getComputedStyle(document.querySelector(".viz-root")).getPropertyValue(name).trim();
  const SERIES = () => [css("--series-1"), css("--series-2"), css("--series-3"), css("--series-4")];

  const fmt = (v, unit) => {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    if (unit === "x") return v.toFixed(2) + "x";
    return v >= 1000 ? (v / 1000).toFixed(2) + " s" : Math.round(v) + " ms";
  };

  function el(name, attrs, text) {
    const node = document.createElementNS(NS, name);
    for (const k in attrs) node.setAttribute(k, attrs[k]);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function hover(node, htmlText) {
    node.addEventListener("mousemove", (e) => {
      tip.innerHTML = htmlText;
      tip.style.opacity = "1";
      const pad = 14;
      let x = e.clientX + pad, y = e.clientY + pad;
      const r = tip.getBoundingClientRect();
      if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - pad;
      if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - pad;
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    });
    node.addEventListener("mouseleave", () => { tip.style.opacity = "0"; });
  }

  // ── Header ─────────────────────────────────────────────────────────
  document.getElementById("title").textContent = D.spec.name;
  const gpus = (D.environment.gpus || []).join("; ");
  document.getElementById("subtitle").textContent =
    D.spec.concurrency + " concurrent session(s), " + D.spec.turns + " turns each, " +
    D.spec.warmup_turns + " warmup excluded · " + (D.environment.host || "") +
    (gpus ? " · " + gpus : "");

  const byName = {};
  (D.stats.headline || []).concat(D.stats.stages || []).forEach(d => byName[d.name] = d);

  const tiles = [
    ["Perceived TTFA p50", byName.perceived_ttfa ? fmt(byName.perceived_ttfa.percentiles["50"]) : "—", "end of speech → first audio"],
    ["Perceived TTFA p95", byName.perceived_ttfa ? fmt(byName.perceived_ttfa.percentiles["95"]) : "—", "tail a user still hears"],
    ["Completed", (D.stats.completion_rate * 100).toFixed(0) + "%", D.stats.completed_turns + " of " + D.stats.total_turns + " turns"],
    ["Realtime factor", D.stats.realtime_factor ? fmt(D.stats.realtime_factor.percentiles["50"], "x") : "—", "median; >1.0x sustains speech"],
  ];
  document.getElementById("tiles").innerHTML = tiles.map(
    t => '<div class="tile"><div class="k">' + t[0] + '</div><div class="v">' + t[1] + '</div><div class="d">' + t[2] + '</div></div>'
  ).join("");

  // ── Budget: one stacked horizontal bar, direct-labelled ────────────
  function drawBudget() {
    const host = document.getElementById("budget");
    host.innerHTML = "";
    const data = D.budget || [];
    if (!data.length) { host.innerHTML = '<p class="note">No completed turn to decompose.</p>'; return; }
    const colors = SERIES();
    const W = host.clientWidth || 880, H = 150;
    const L = 8, R = 8, barY = 54, barH = 24, GAP = 2;
    const total = data.reduce((a, b) => a + b.ms, 0);
    const scale = (W - L - R) / total;

    const svg = el("svg", { width: "100%", height: H, viewBox: "0 0 " + W + " " + H });
    let x = L;
    data.forEach((d, i) => {
      const raw = d.ms * scale;
      const w = Math.max(2, raw - (i < data.length - 1 ? GAP : 0));
      // 4px rounded outer ends, square where segments meet.
      const first = i === 0, last = i === data.length - 1;
      const r = 4;
      const path = first || last
        ? roundedSide(x, barY, w, barH, r, first, last)
        : rectPath(x, barY, w, barH);
      const mark = el("path", { d: path, fill: colors[i % 4], class: "mark" });
      hover(mark, "<b>" + d.label + "</b><br>" + fmt(d.ms) + " · " + Math.round(d.share * 100) + "% of " + fmt(total));
      svg.appendChild(mark);

      if (raw > 46) {
        svg.appendChild(el("text", {
          x: x + w / 2, y: barY - 10, "text-anchor": "middle", class: "axis"
        }, fmt(d.ms)));
      }
      x += raw;
    });
    svg.appendChild(el("text", { x: L, y: barY + barH + 22, class: "axis" }, "0"));
    svg.appendChild(el("text", { x: W - R, y: barY + barH + 22, "text-anchor": "end", class: "axis" },
      fmt(total) + " to first audio"));
    host.appendChild(svg);

    document.getElementById("budget-legend").innerHTML = data.map(
      (d, i) => '<span><i class="swatch" style="background:' + colors[i % 4] + '"></i>' + d.label + " · " + fmt(d.ms) + "</span>"
    ).join("");

    document.getElementById("budget-table").innerHTML =
      "<tr><th>Stage</th><th>Time</th><th>Share</th></tr>" +
      data.map(d => "<tr><td>" + d.label + "</td><td>" + fmt(d.ms) + "</td><td>" + Math.round(d.share * 100) + "%</td></tr>").join("") +
      "<tr><td><b>Total</b></td><td><b>" + fmt(total) + "</b></td><td></td></tr>";
  }

  function rectPath(x, y, w, h) {
    return "M" + x + "," + y + "h" + w + "v" + h + "h" + (-w) + "z";
  }
  function roundedSide(x, y, w, h, r, left, right) {
    r = Math.min(r, w / 2, h / 2);
    const rl = left ? r : 0, rr = right ? r : 0;
    return "M" + (x + rl) + "," + y +
      "h" + (w - rl - rr) +
      (rr ? "a" + rr + "," + rr + " 0 0 1 " + rr + "," + rr : "") +
      "v" + (h - 2 * rr) +
      (rr ? "a" + rr + "," + rr + " 0 0 1 " + (-rr) + "," + rr : "") +
      "h" + (-(w - rl - rr)) +
      (rl ? "a" + rl + "," + rl + " 0 0 1 " + (-rl) + "," + (-rl) : "") +
      "v" + (-(h - 2 * rl)) +
      (rl ? "a" + rl + "," + rl + " 0 0 1 " + rl + "," + (-rl) : "") + "z";
  }

  // ── Stage percentiles: grouped horizontal bars ─────────────────────
  function drawStages() {
    const host = document.getElementById("stages");
    host.innerHTML = "";
    const rows = D.stats.stages || [];
    if (!rows.length) { host.innerHTML = '<p class="note">No stage data.</p>'; return; }
    const colors = SERIES();
    const W = host.clientWidth || 880;
    const rowH = 46, L = 168, R = 70, H = rows.length * rowH + 30;
    const max = Math.max.apply(null, rows.map(r => r.percentiles["95"]));
    const scale = (W - L - R) / (max || 1);
    const svg = el("svg", { width: "100%", height: H, viewBox: "0 0 " + W + " " + H });

    rows.forEach((row, i) => {
      const y = i * rowH + 12;
      svg.appendChild(el("text", { x: L - 12, y: y + 17, "text-anchor": "end", class: "axis" }, row.label));
      const p50 = row.percentiles["50"] * scale;
      const p95 = row.percentiles["95"] * scale;
      // p95 sits behind p50 as a lighter extent; p50 is the solid mark.
      const back = el("path", { d: roundedSide(L, y + 4, Math.max(2, p95), 22, 4, true, true), fill: colors[i % 4], opacity: "0.26", class: "mark" });
      hover(back, "<b>" + row.label + " p95</b><br>" + fmt(row.percentiles["95"], row.unit));
      svg.appendChild(back);
      const front = el("path", { d: roundedSide(L, y + 4, Math.max(2, p50), 22, 4, true, true), fill: colors[i % 4], class: "mark" });
      hover(front, "<b>" + row.label + "</b><br>p50 " + fmt(row.percentiles["50"], row.unit) +
        "<br>p90 " + fmt(row.percentiles["90"], row.unit) +
        "<br>p95 " + fmt(row.percentiles["95"], row.unit) +
        "<br>n = " + row.count);
      svg.appendChild(front);
      svg.appendChild(el("text", { x: L + Math.max(p95, p50) + 10, y: y + 20, class: "axis" }, fmt(row.percentiles["50"], row.unit)));
    });
    svg.appendChild(el("line", { x1: L, y1: 8, x2: L, y2: H - 18, stroke: css("--baseline"), "stroke-width": "1" }));
    svg.appendChild(el("text", { x: L, y: H - 4, class: "axis" }, "solid = p50, pale = p95"));
    host.appendChild(svg);
  }

  // ── Per-turn line ──────────────────────────────────────────────────
  function drawTurns() {
    const host = document.getElementById("turns");
    host.innerHTML = "";
    const pts = (D.turns || []).map((t, i) => ({ i: i, v: t.latency_ms.perceived_ttfa, t: t }))
      .filter(p => p.v !== null && p.v !== undefined);
    if (pts.length < 2) { host.innerHTML = '<p class="note">Not enough completed turns to plot.</p>'; return; }
    const W = host.clientWidth || 880, H = 220, L = 60, R = 16, T = 14, B = 30;
    const max = Math.max.apply(null, pts.map(p => p.v)) * 1.1;
    const xs = (i) => L + (W - L - R) * (i / Math.max(1, pts.length - 1));
    const ys = (v) => T + (H - T - B) * (1 - v / max);
    const svg = el("svg", { width: "100%", height: H, viewBox: "0 0 " + W + " " + H });

    for (let g = 0; g <= 4; g++) {
      const v = max * g / 4, y = ys(v);
      svg.appendChild(el("line", { x1: L, y1: y, x2: W - R, y2: y, stroke: css("--grid"), "stroke-width": "1" }));
      svg.appendChild(el("text", { x: L - 10, y: y + 4, "text-anchor": "end", class: "axis" }, fmt(v)));
    }
    const d = pts.map((p, i) => (i ? "L" : "M") + xs(p.i) + "," + ys(p.v)).join("");
    svg.appendChild(el("path", { d: d, fill: "none", stroke: SERIES()[0], "stroke-width": "2", "stroke-linejoin": "round", "stroke-linecap": "round" }));
    pts.forEach(p => {
      const c = el("circle", { cx: xs(p.i), cy: ys(p.v), r: "4.5", fill: SERIES()[0], stroke: css("--surface-1"), "stroke-width": "2", class: "mark" });
      hover(c, "<b>Turn " + (p.i + 1) + "</b><br>perceived " + fmt(p.v) +
        "<br>ASR " + fmt(p.t.latency_ms.asr) +
        "<br>LLM " + fmt(p.t.latency_ms.llm_ttft) +
        "<br>TTS " + fmt(p.t.latency_ms.tts_ttfb));
      svg.appendChild(c);
    });
    svg.appendChild(el("text", { x: L, y: H - 6, class: "axis" }, "turn order →"));
    host.appendChild(svg);
  }

  // ── Table ──────────────────────────────────────────────────────────
  const all = (D.stats.headline || []).concat(D.stats.stages || []);
  document.getElementById("table").innerHTML =
    "<tr><th>Measure</th><th>n</th><th>p50</th><th>p90</th><th>p95</th><th>p99</th><th>max</th></tr>" +
    all.map(d => "<tr><td>" + d.label + "</td><td>" + d.count + "</td>" +
      ["50", "90", "95", "99"].map(q => "<td>" + fmt(d.percentiles[q], d.unit) + "</td>").join("") +
      "<td>" + fmt(d.max, d.unit) + "</td></tr>").join("");

  function drawAll() { drawBudget(); drawStages(); drawTurns(); }
  drawAll();
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawAll, 120); });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", drawAll);
  }
})();
</script>
</body>
</html>
"""
