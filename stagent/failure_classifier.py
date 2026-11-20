"""失败根因自动分类器 v1：纯规则引擎，不调 LLM。

设计原则：
1. 快（毫秒级）、可复现——同一 step 序列每次分类结果相同
2. 不做"软判定"——confidence=1.0 表示命中规则，0.0 表示默认
3. 规则按优先级排序——一个 step 序列可能命中多条，但取最具体的

分类枚举（v1 做 5 类，够 Phase 2 用）：
  front_end_bug       —— 页面自身 bug（表单值被清 / 按钮失效 / 死循环）
  agent_loop          —— agent 陷入循环（loop_hint 连续触发）
  agent_timeout       —— timeout_s 杀停
  agent_max_steps     —— max_steps 杀停
  assertion_judge     —— agent 正常走完，但裁判判定断言失败（可能 agent 没推进到位）
  agent_strategy      —— 工具返回 error / 自愈失败（agent 应该换策略）
  unknown             —— 都不命中

用法：
  from stagent.failure_classifier import classify
  fc = classify(case_result)  # CaseResult → FailureClassification dataclass
  print(fc.to_dict())
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .agent import CaseResult


@dataclass
class FailureClassification:
    failure_category: str = "unknown"
    confidence: float = 0.0
    reason: str = ""
    signal_count: int = 0
    matched_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_category": self.failure_category,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "signal_count": self.signal_count,
            "matched_signals": list(self.matched_signals),
        }


def classify(result: CaseResult) -> FailureClassification:
    """对一个 CaseResult 做失败分类。result.status != "passed" 时调用有意义。"""
    sigs = _collect_signals(result)
    return _decide(sigs, result)


# =====================================================================
# 信号采集
# =====================================================================

def _collect_signals(result: CaseResult) -> dict[str, Any]:
    """收集 CaseResult 里所有可判定信号。"""
    steps = result.steps

    has_loop_hint = any(s.loop_hint for s in steps)
    has_timeout_error = bool(result.error and "timeout" in result.error.lower())
    has_max_steps_hit = bool(
        isinstance(result.answer, str) and "达到最大步数" in result.answer
    )

    # 自愈统计
    heal_total = sum(1 for s in steps if s.healed)
    tool_error_steps = [s for s in steps if not s.ok and s.error]

    # Dismiss error 模式：Dismiss 按钮点击 + 前后有 error 类 step
    dismiss_clicks = [
        s for s in steps
        if s.tool == "browser_click"
        and isinstance(s.args, dict)
        and "dismiss" in str(s.args.get("element", "")).lower()
    ]
    dismiss_count = len(dismiss_clicks)

    # "值被清掉"模式：fill_form → Continue → dismiss → 重填 → Continue → dismiss
    # 用 step 序列滑动窗口匹配
    form_clear_pattern = _detect_form_clear_pattern(steps)

    # evaluate / run_code_unsafe 死循环（已被循环检测覆盖，但作为信号）
    consecutive_eval = _count_consecutive_similar(steps, "browser_evaluate")
    consecutive_run_code = _count_consecutive_similar(steps, "browser_run_code_unsafe")
    consecutive_type = _count_consecutive_similar(steps, "browser_type") + _count_consecutive_similar(steps, "browser_press_key")

    # assertion 失败统计
    failed_assertions = [a for a in result.assertions if not a.get("passed")]

    # 步数 / 用时
    total_steps = len(steps)
    duration_s = result.duration_s

    # Phase 2: 慢环境信号——CI runner 上 tool duration 平均 > 1.5s 或最慢 > 5s
    slow_steps = [s for s in steps if (getattr(s, "duration_s", 0) or 0) > 2.0]
    avg_step_s = (sum(getattr(s, "duration_s", 0) or 0 for s in steps) / total_steps) if total_steps else 0
    ok_ratio = (sum(1 for s in steps if s.ok) / total_steps) if total_steps else 0

    return {
        "has_loop_hint": has_loop_hint,
        "has_timeout_error": has_timeout_error,
        "has_max_steps_hit": has_max_steps_hit,
        "heal_total": heal_total,
        "tool_error_count": len(tool_error_steps),
        "dismiss_count": dismiss_count,
        "form_clear_pattern": form_clear_pattern,
        "consecutive_eval": consecutive_eval,
        "consecutive_run_code": consecutive_run_code,
        "consecutive_type": consecutive_type,
        "failed_assertion_count": len(failed_assertions),
        "total_steps": total_steps,
        "duration_s": duration_s,
        "result_status": result.status,
        # Phase 2: timing signals
        "slow_step_count": len(slow_steps),
        "avg_step_s": round(avg_step_s, 2),
        "ok_ratio": round(ok_ratio, 2),
    }


def _count_consecutive_similar(steps, tool_name: str) -> int:
    """统计该工具连续出现的最大次数。"""
    max_count = 0
    cur = 0
    last_fp = ""
    for s in steps:
        if s.tool != tool_name:
            cur = 0
            continue
        fp = _step_fingerprint(s)
        if fp == last_fp:
            cur += 1
        else:
            cur = 1
        max_count = max(max_count, cur)
        last_fp = fp
    return max_count


def _step_fingerprint(step) -> str:
    """简单指纹：args 里排除 target/similarity 后的 JSON。"""
    import json as _json
    args = dict(step.args or {})
    for k in ("target", "similarity"):
        args.pop(k, None)
    try:
        return _json.dumps(args, sort_keys=True, ensure_ascii=False)[:100]
    except Exception:
        return str(args)[:100]


def _detect_form_clear_pattern(steps) -> int:
    """检测 fill_form → Continue → dismiss → 重填 → Continue → dismiss 模式。
    返回匹配次数（至少 2 次算命中）。"""
    patterns = [
        # fill_form → click(Continue) → dismiss click
        ["browser_fill_form", "browser_click", "browser_click"],
    ]
    hit = 0
    for i in range(len(steps) - 2):
        s1, s2, s3 = steps[i], steps[i + 1], steps[i + 2]
        if s1.tool != "browser_fill_form":
            continue
        if s2.tool != "browser_click" or not isinstance(s2.args, dict) or "continue" not in str(s2.args.get("element", "")).lower():
            continue
        if s3.tool != "browser_click" or not isinstance(s3.args, dict) or "dismiss" not in str(s3.args.get("element", "")).lower():
            continue
        hit += 1
    return hit


# =====================================================================
# 决策（按优先级排序，一个分类只在命中所有信号时触发）
# =====================================================================

def _decide(sigs: dict[str, Any], result: CaseResult) -> FailureClassification:
    matched: list[str] = []

    # ── 最高优先级：前端 bug 明确信号 ──
    if sigs["form_clear_pattern"] >= 2 or sigs["dismiss_count"] >= 2:
        matched.append("form_clear_pattern")
        matched.append(f"dismiss_count={sigs['dismiss_count']}")
        if sigs["form_clear_pattern"] >= 2:
            matched.append(f"form_clear_hits={sigs['form_clear_pattern']}")
        return FailureClassification(
            failure_category="front_end_bug",
            confidence=0.92,
            reason=(
                f"检测到 Checkout 表单值被前端 bug 反复清掉（fill_form→Continue→dismiss 模式 "
                f"出现 {sigs['form_clear_pattern']} 次，Dismiss 按钮点击 {sigs['dismiss_count']} 次）。"
                f"这是 Saucedemo known bug——problem_user / error_user 等账号的 Checkout 表单有前端 bug。"
            ),
            signal_count=len(matched),
            matched_signals=matched,
        )

    # ── run_code_unsafe / evaluate 死循环 ──
    if sigs["consecutive_run_code"] >= 3 or sigs["consecutive_eval"] >= 3:
        matched.append("tool_loop")
        matched.append(f"run_code_unsafe={sigs['consecutive_run_code']}")
        matched.append(f"evaluate={sigs['consecutive_eval']}")
        return FailureClassification(
            failure_category="front_end_bug",
            confidence=0.85,
            reason=(
                f"LLM 陷入后门工具死循环：run_code_unsafe 连续 {sigs['consecutive_run_code']} 次、"
                f"browser_evaluate 连续 {sigs['consecutive_eval']} 次。"
                f"通常是前端 bug 迫使 agent 绕过 MCP 协议层尝试原生 Playwright locator。"
            ),
            signal_count=len(matched),
            matched_signals=matched,
        )

    # ── timeout 杀停 ──
    if sigs["has_timeout_error"]:
        matched.append("timeout")
        return FailureClassification(
            failure_category="agent_timeout",
            confidence=0.88,
            reason=f"用例被 timeout_s={_get_timeout(result)} 杀停，用时 {sigs['duration_s']}s / {sigs['total_steps']} 步。",
            signal_count=1,
            matched_signals=matched,
        )

    # ── max_steps 杀停 ──
    if sigs["has_max_steps_hit"]:
        # Phase 2: 如果 tool 几乎全 ok + 没 loop_hint + 有慢 step → timing_issue
        # 说明 agent 在 CI 慢环境下一直在推进，只是步数预算不够
        if sigs["ok_ratio"] >= 0.8 and not sigs["has_loop_hint"] and sigs["slow_step_count"] >= 3:
            matched.append("max_steps")
            matched.append(f"ok_ratio={sigs['ok_ratio']}")
            matched.append(f"slow_steps={sigs['slow_step_count']}")
            matched.append(f"avg_step_s={sigs['avg_step_s']}")
            return FailureClassification(
                failure_category="timing_issue",
                confidence=0.78,
                reason=(
                    f"max_steps 杀停但 {sigs['ok_ratio']*100:.0f}% 的工具调用成功，"
                    f"且有 {sigs['slow_step_count']} 个 step 慢于 2s（平均 {sigs['avg_step_s']}s/步）。"
                    f"agent 在 CI runner 上一直在推进，只是执行太慢导致步数预算耗尽。"
                    f"建议：case 里加 max_steps_relief 或全局调大 agent.max_steps。"
                ),
                signal_count=len(matched),
                matched_signals=matched,
            )

        matched.append("max_steps")
        return FailureClassification(
            failure_category="agent_max_steps",
            confidence=0.85,
            reason=f"用例达到 max_steps 仍未见结论，执行 {sigs['total_steps']} 步。",
            signal_count=1,
            matched_signals=matched,
        )

    # ── loop_hint 触发 ──
    if sigs["has_loop_hint"]:
        matched.append("loop_hint")
        if sigs["consecutive_type"] >= 3:
            matched.append(f"type_loop={sigs['consecutive_type']}")
        return FailureClassification(
            failure_category="agent_loop",
            confidence=0.75,
            reason=(
                f"循环检测触发，agent 陷入重复操作。"
                f"连续相似工具最高 {max(sigs['consecutive_type'], sigs['consecutive_eval'])} 次。"
                f"可能是前端 bug 导致 agent 不停重试同一动作。"
            ),
            signal_count=len(matched),
            matched_signals=matched,
        )

    # ── tool error / 自愈失败 ──
    if sigs["tool_error_count"] > 0 and sigs["heal_total"] == 0:
        matched.append("tool_error_no_heal")
        return FailureClassification(
            failure_category="agent_strategy",
            confidence=0.65,
            reason=(
                f"{sigs['tool_error_count']} 个工具调用返回 error，且自愈全部未触发。"
                f"agent 应该换策略（换 browser_type / 换 ref）。"
            ),
            signal_count=1,
            matched_signals=matched,
        )

    # ── 自愈也救不了 ──
    if sigs["tool_error_count"] > 0 and sigs["heal_total"] > 0:
        matched.append("tool_error_heal_failed")
        return FailureClassification(
            failure_category="agent_strategy",
            confidence=0.70,
            reason=(
                f"{sigs['tool_error_count']} 个工具调用返回 error，自愈尝试 {sigs['heal_total']} 次后仍失败。"
                f"healer 也救不了——可能需要换一种元素定位策略。"
            ),
            signal_count=1,
            matched_signals=matched,
        )

    # ── 断言失败（agent 正常走完但裁判说不过） ──
    if sigs["failed_assertion_count"] > 0 and sigs["result_status"] == "failed":
        matched.append("assertion_failed")
        return FailureClassification(
            failure_category="assertion_judge",
            confidence=0.60,
            reason=(
                f"agent 正常执行完 {sigs['total_steps']} 步，但独立断言裁判判定 "
                f"{sigs['failed_assertion_count']} 条断言失败。"
                f"可能 agent 没正确推进（页面状态未满足断言），或断言本身过于严格。"
            ),
            signal_count=1,
            matched_signals=matched,
        )

    # ── 兜底 ──
    return FailureClassification(
        failure_category="unknown",
        confidence=0.0,
        reason=f"未命中任何分类规则。status={sigs['result_status']}，{sigs['total_steps']} 步。",
        signal_count=0,
        matched_signals=[],
    )


def _get_timeout(result: CaseResult) -> str:
    """从 CaseResult 里试着拿 timeout_s（CaseResult 没存 timeout，从 error 文本反推）。"""
    if result.error:
        m = re.search(r">\s*([\d.]+)s", result.error)
        if m:
            return m.group(1)
    return "?"
