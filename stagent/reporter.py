"""报告生成：Markdown（给人看）+ JSON（给流水线看）+ HTML（带截图）。"""
from __future__ import annotations

import html
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent import CaseResult

STATUS_ICON = {
    "passed": "✅",
    "failed": "❌",
    "error": "💥",
    "completed": "⬜",
}
STATUS_TEXT = {
    "passed": "通过",
    "failed": "断言未通过",
    "error": "执行异常",
    "completed": "已执行（未配置断言）",
}


def _summarize(results: list[CaseResult]) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "error": 0, "completed": 0}
    counts["expected_fail"] = 0
    counts["unexpected_fail"] = 0
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1
        if item.status == "passed":
            continue
        if item.expected_fail:
            counts["expected_fail"] += 1
        else:
            counts["unexpected_fail"] += 1
    return counts


def write_reports(
    out_dir: Path,
    title: str,
    results: list[CaseResult],
    meta: dict[str, Any] | None = None,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = meta or {}
    counts = _summarize(results)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    md_path = out_dir / "report.md"
    json_path = out_dir / "summary.json"
    html_path = out_dir / "report.html"

    md_path.write_text(
        _render_markdown(title, results, counts, meta, stamp, out_dir), encoding="utf-8"
    )
    html_path.write_text(
        _render_html(title, results, counts, meta, stamp, out_dir), encoding="utf-8"
    )
    json_path.write_text(
        json.dumps(
            {
                "title": title,
                "generated_at": stamp,
                "meta": meta,
                "counts": counts,
                "cases": [_case_to_dict(r, out_dir) for r in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"markdown": md_path, "html": html_path, "json": json_path}


def _case_to_dict(result: CaseResult, out_dir: Path) -> dict[str, Any]:
    data = asdict(result)
    data["screenshots"] = [_rel(p, out_dir) for p in result.screenshots]
    for step in data.get("steps", []):
        step["screenshots"] = [_rel(p, out_dir) for p in step.get("screenshots", [])]
    return data


def _rel(path: str | Path, base: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(base.resolve()))
    except Exception:
        return str(path)


def _render_markdown(
    title: str,
    results: list[CaseResult],
    counts: dict[str, int],
    meta: dict[str, Any],
    stamp: str,
    out_dir: Path,
) -> str:
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 生成时间 | {stamp} |")
    lines.append(f"| 模型 | {meta.get('model', '-')} |")
    lines.append(f"| 浏览器 | Playwright MCP（{meta.get('headless', '-')}）|")
    lines.append(f"| 用例总数 | {len(results)} |")
    ef = counts.get("expected_fail", 0)
    uf = counts.get("unexpected_fail", 0)
    lines.append(
        f"| 通过 / ❌意外失败 / ⚠️预期失败 / 异常 | "
        f"{counts['passed']} / {uf} / {ef} / {counts['error']} |"
    )
    lines.append("")

    for result in results:
        # 三标记：passed✅ / expected_fail且失败→⚠️ / 其它失败→❌ / error→💥
        if result.status == "passed":
            icon = "✅"
        elif result.status == "error":
            icon = "💥"
        elif result.expected_fail and result.status in ("failed", "completed"):
            icon = "⚠️"
        elif result.status == "failed":
            icon = "❌"
        else:
            icon = STATUS_ICON.get(result.status, "⬜")
        lines.append(f"## {icon} {result.name}")
        lines.append("")
        lines.append(f"- 状态：{STATUS_TEXT.get(result.status, result.status)}")
        if result.expected_fail and result.expected_fail_reason:
            lines.append(f"- ⚠️ 预期失败原因：{result.expected_fail_reason}")
        lines.append(f"- 耗时：{result.duration_s}s")
        if result.url:
            lines.append(f"- 起始地址：{result.url}")
        if result.error:
            lines.append(f"- 异常：`{result.error}`")
        # ★ Phase 2.1：失败分类（passed 不显示）
        if result.status != "passed" and result.failure_category:
            conf = f"{result.failure_confidence:.0%}" if result.failure_confidence else ""
            cat = result.failure_category.replace("_", " ").title()
            lines.append(f"- 🔍 失败分类：`{cat}`{('（置信度 ' + conf + '）') if conf else ''}")
            if result.failure_reason:
                lines.append(f"- 分类依据：{result.failure_reason}")
        lines.append("")

        if result.assertions:
            lines.append("### 断言")
            lines.append("")
            lines.append("| # | 断言 | 结果 | 证据 |")
            lines.append("| --- | --- | --- | --- |")
            for i, item in enumerate(result.assertions, start=1):
                ok = "通过" if item.get("passed") else "未通过"
                assertion = str(item.get("assertion", "")).replace("|", "\\|")
                evidence = str(item.get("evidence", "")).replace("|", "\\|").replace("\n", " ")[:120]
                lines.append(f"| {i} | {assertion} | {ok} | {evidence} |")
            lines.append("")

        if result.steps:
            lines.append("### 执行步骤")
            lines.append("")
            lines.append("| # | 工具 | 参数 | 结果 | 自愈 | 耗时 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for step in result.steps:
                args = json.dumps(step.args, ensure_ascii=False).replace("|", "\\|")[:120]
                outcome = "成功" if step.ok else "失败"
                healed = "是" if step.healed else ""
                lines.append(
                    f"| {step.index} | `{step.tool}` | `{args}` | {outcome} | {healed} | {step.duration_s}s |"
                )
            lines.append("")

        if result.healings:
            lines.append("### 自愈记录")
            lines.append("")
            for event in result.healings:
                mark = "✔" if event.get("ok") else "✘"
                lines.append(
                    f"- {mark} 尝试{event.get('attempt')}：{event.get('reason', '')}"
                    + (f" → ref={event.get('ref')}" if event.get("ref") else "")
                )
            lines.append("")

        if result.answer:
            lines.append("### Agent 结论")
            lines.append("")
            lines.append("```text")
            lines.append(result.answer.strip())
            lines.append("```")
            lines.append("")

        if result.screenshots:
            lines.append("### 截图")
            lines.append("")
            for shot in result.screenshots:
                rel = _rel(shot, out_dir)
                lines.append(f"![{Path(shot).name}]({rel})")
            lines.append("")

    return "\n".join(lines)


def _render_html(
    title: str,
    results: list[CaseResult],
    counts: dict[str, int],
    meta: dict[str, Any],
    stamp: str,
    out_dir: Path,
) -> str:
    ef = counts.get("expected_fail", 0)
    uf = counts.get("unexpected_fail", 0)
    total = len(results)
    pass_rate = counts["passed"] / total * 100 if total else 0
    total_time = sum(r.duration_s for r in results)
    slowest = max(results, key=lambda r: r.duration_s) if results else None
    fastest = min(results, key=lambda r: r.duration_s) if results else None

    body: list[str] = []

    # ── 汇总面板 ──
    body.append('<div class="summary-panel">')
    body.append(f'<div class="pass-rate">{pass_rate:.0f}%<span class="pass-rate-label">通过率</span></div>')
    body.append('<div class="stat-grid">')
    body.append(f'<div class="stat"><div class="stat-num passed">{counts["passed"]}</div><div class="stat-label">通过</div></div>')
    body.append(f'<div class="stat"><div class="stat-num unexpected">{uf}</div><div class="stat-label">意外失败</div></div>')
    body.append(f'<div class="stat"><div class="stat-num expected">{ef}</div><div class="stat-label">预期失败</div></div>')
    body.append(f'<div class="stat"><div class="stat-num error">{counts["error"]}</div><div class="stat-label">异常</div></div>')
    body.append(f'<div class="stat"><div class="stat-num">{total_time:.0f}s</div><div class="stat-label">总耗时</div></div>')
    body.append('</div>')

    # 快速导航标签（失败 case 一键跳转）
    if uf > 0 or counts["error"] > 0:
        body.append('<div class="nav-row">')
        body.append('<span class="nav-title">⚠️ 快速定位：</span>')
        for i, r in enumerate(results):
            if (r.status == "failed" and not r.expected_fail) or r.status == "error":
                body.append(f'<a href="#case-{i+1}" class="nav-link bad">{html.escape(r.name[:30])}</a>')
        body.append('</div>')
    body.append('</div>')  # summary-panel close

    # ── Header ──
    body.append(
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="meta">生成时间 {html.escape(stamp)} ｜ 模型 {html.escape(str(meta.get("model", "-")))} ｜ '
        f"通过 {counts['passed']} ／ ❌意外失败 {uf} ／ ⚠️预期失败 {ef} ／ 异常 {counts['error']}"
    )
    if slowest and fastest and fastest != slowest:
        body.append(
            f'<p class="meta">🕐 最快 {fastest.duration_s:.1f}s（{html.escape(fastest.name[:25])}）｜'
            f" 🐢 最慢 {slowest.duration_s:.1f}s（{html.escape(slowest.name[:25])}）</p>"
        )

    # ── 每个 Case ──
    for i, result in enumerate(results):
        body.append(f'<section class="case" id="case-{i+1}">')
        if result.status == "passed":
            icon = "✅"
            badge_cls = "passed"
        elif result.status == "error":
            icon = "💥"
            badge_cls = "error"
        elif result.expected_fail and result.status in ("failed", "completed"):
            icon = "⚠️"
            badge_cls = "expected-fail"
        elif result.status == "failed":
            icon = "❌"
            badge_cls = "failed"
        else:
            icon = STATUS_ICON.get(result.status, "⬜")
            badge_cls = result.status
        body.append(
            f'<h2><span class="badge {badge_cls}">{icon} '
            f"{html.escape(STATUS_TEXT.get(result.status, result.status))}</span> {html.escape(result.name)}</h2>"
        )
        if result.expected_fail and result.expected_fail_reason:
            body.append(f'<p class="expected-reason">⚠️ 预期失败：{html.escape(result.expected_fail_reason)}</p>')
        body.append(f'<p class="meta">耗时 {result.duration_s}s ｜ 地址 {html.escape(result.url or "-")}</p>')
        if result.error:
            body.append(f'<p class="err">{html.escape(result.error)}</p>')
        # ★ Phase 2.1：失败分类（passed 不显示）
        if result.status != "passed" and result.failure_category:
            conf = f"{result.failure_confidence:.0%}" if result.failure_confidence else ""
            cat = result.failure_category.replace("_", " ").title()
            body.append(
                f'<p class="failure-category">🔍 失败分类：<code>{html.escape(cat)}</code>'
                f'{("（置信度 " + conf + "）") if conf else ""}</p>'
            )
            if result.failure_reason:
                body.append(
                    f'<p class="failure-reason">{html.escape(result.failure_reason)}</p>'
                )

        if result.assertions:
            body.append("<h3>断言</h3><ul class='assertions'>")
            for item in result.assertions:
                cls = "ok" if item.get("passed") else "bad"
                body.append(
                    f'<li class="{cls}">{"✔" if item.get("passed") else "✘"} '
                    f"{html.escape(str(item.get('assertion', '')))}"
                    f"<em>证据：{html.escape(str(item.get('evidence', ''))[:200])}</em></li>"
                )
            body.append("</ul>")

        if result.steps:
            body.append("<h3>执行步骤</h3><table><tr><th>#</th><th>工具</th><th>参数</th><th>结果</th><th>耗时</th></tr>")
            for step in result.steps:
                args = html.escape(json.dumps(step.args, ensure_ascii=False))[:160]
                body.append(
                    f"<tr><td>{step.index}</td><td><code>{html.escape(step.tool)}</code></td>"
                    f"<td><code>{args}</code></td><td>{'成功' if step.ok else '失败'}"
                    f"{'（自愈）' if step.healed else ''}</td><td>{step.duration_s}s</td></tr>"
                )
            body.append("</table>")

        if result.answer:
            body.append(f"<h3>Agent 结论</h3><pre>{html.escape(result.answer.strip())}</pre>")

        if result.screenshots:
            body.append("<h3>📸 截图 Timeline</h3><div class='shots-timeline'>")
            for idx, shot in enumerate(result.screenshots):
                rel = html.escape(_rel(shot, out_dir))
                # 从截图文件名提取步骤号（step_01_xxx.png → Step 1）
                label = Path(shot).stem
                step_match = __import__("re").match(r"step_(\d+)", label)
                step_num = step_match.group(1) if step_match else str(idx + 1)
                body.append(
                    f'<div class="shot-item">'
                    f'<span class="shot-label">Step {step_num}: {html.escape(label)}</span>'
                    f'<a href="{rel}" target="_blank"><img src="{rel}" alt="Step {step_num}"></a>'
                    f'</div>'
                )
            body.append("</div>")

        body.append("</section>")

    return _HTML_TEMPLATE.format(title=html.escape(title), body="\n".join(body))


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{ --passed:#166534; --passed-bg:#dcfce7; --failed:#991b1b; --failed-bg:#fee2e2; --expected:#92400e; --expected-bg:#fef3c7; --error:#7c2d12; --error-bg:#ffedd5; --border:#e5e7eb; --muted:#6b7280; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 32px auto; max-width: 1100px; color: #1f2328; line-height: 1.6; }}
  h1 {{ font-size: 24px; margin-top: 0; }}
  h2 {{ font-size: 18px; margin-top: 32px; border-bottom: 1px solid #e5e7eb; padding-bottom: 8px; scroll-margin-top: 12px; }}
  h3 {{ font-size: 15px; margin-top: 20px; }}
  .meta {{ color: #6b7280; font-size: 13px; margin: 4px 0; }}

  /* ── 汇总面板 ── */
  .summary-panel {{ background: linear-gradient(135deg, #f0f9ff 0%, #faf5ff 100%); border: 1px solid #e0e7ff; border-radius: 14px; padding: 24px; margin: 0 0 24px; }}
  .pass-rate {{ font-size: 56px; font-weight: 700; color: var(--passed); line-height: 1; text-align: center; }}
  .pass-rate-label {{ font-size: 14px; color: var(--muted); display: block; margin-top: 4px; font-weight: 400; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; margin-top: 20px; }}
  .stat {{ text-align: center; }}
  .stat-num {{ font-size: 28px; font-weight: 700; color: #1f2328; }}
  .stat-num.passed {{ color: var(--passed); }}
  .stat-num.unexpected {{ color: var(--failed); }}
  .stat-num.expected {{ color: var(--expected); }}
  .stat-num.error {{ color: var(--error); }}
  .stat-label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }}
  .nav-row {{ margin-top: 16px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  .nav-title {{ font-size: 13px; color: var(--muted); }}
  .nav-link {{ font-size: 12px; padding: 4px 10px; border-radius: 6px; text-decoration: none; }}
  .nav-link.bad {{ background: #fee2e2; color: var(--failed); border: 1px solid #fecaca; }}
  .nav-link.bad:hover {{ background: #fecaca; }}

  /* ── Case 卡片 ── */
  .case {{ border: 1px solid var(--border); border-radius: 10px; padding: 4px 20px 16px; margin: 20px 0; }}
  .badge {{ font-size: 13px; padding: 2px 8px; border-radius: 999px; margin-right: 8px; }}
  .badge.passed {{ background: var(--passed-bg); color: var(--passed); }}
  .badge.failed {{ background: var(--failed-bg); color: var(--failed); }}
  .badge.error {{ background: var(--error-bg); color: var(--error); }}
  .badge.expected-fail {{ background: var(--expected-bg); color: var(--expected); }}
  .badge.completed {{ background: #e5e7eb; color: #374151; }}
  .expected-reason {{ color: var(--expected); background: #fffbeb; padding: 6px 10px; border-radius: 6px; font-size: 13px; margin-top: 4px; border-left: 3px solid #fbbf24; }}
  .failure-category {{ color: #7c3aed; background: #faf5ff; padding: 6px 10px; border-radius: 6px; font-size: 13px; margin-top: 4px; border-left: 3px solid #a78bfa; }}
  .failure-reason {{ color: #4c1d95; background: #f5f3ff; padding: 6px 10px; border-radius: 6px; font-size: 12px; margin-top: 2px; line-height: 1.5; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
  th, td {{ border: 1px solid var(--border); padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #f9fafb; }}
  code {{ font-size: 12px; word-break: break-all; }}
  pre {{ background: #f6f8fa; padding: 12px; border-radius: 8px; white-space: pre-wrap; font-size: 13px; }}
  .assertions {{ list-style: none; padding-left: 0; }}
  .assertions li {{ padding: 6px 0; border-bottom: 1px dashed #eee; }}
  .assertions li.ok {{ color: var(--passed); }}
  .assertions li.bad {{ color: var(--failed); }}
  .assertions em {{ display: block; color: var(--muted); font-size: 12px; font-style: normal; }}
  .err {{ color: var(--failed); background: #fef2f2; padding: 8px; border-radius: 6px; }}

  /* ── 截图 Timeline ── */
  .shots-timeline {{ display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }}
  .shots-timeline .shot-item {{ display: flex; align-items: center; gap: 12px; }}
  .shots-timeline .shot-label {{ flex-shrink: 0; font-size: 12px; color: var(--muted); min-width: 160px; }}
  .shots-timeline img {{ max-width: 200px; border: 1px solid var(--border); border-radius: 6px; cursor: zoom-in; }}
  .shots-grid {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .shots-grid img {{ max-width: 320px; border: 1px solid var(--border); border-radius: 6px; }}

  /* ── 响应式 ── */
  @media (max-width: 720px) {{
    .stat-grid {{ grid-template-columns: repeat(3, 1fr); }}
    .pass-rate {{ font-size: 40px; }}
    body {{ margin: 16px; }}
  }}
</style>
</head>
<body>
{body}
</body>
</html>
"""
