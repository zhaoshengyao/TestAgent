"""独立验证 failure_classifier——用 problem_user 的历史 run 数据做单元测试。

不启动浏览器，直接 import CaseResult + classify。
"""
import sys, json
sys.path.insert(0, ".")

from stagent.agent import CaseResult, StepRecord
from stagent.failure_classifier import classify


def make_steps(tool_counts, has_loop_hint=False):
    """造一个假 step 序列。tool_counts = [(tool_name, count, fingerprint)]。"""
    steps = []
    for i, (tool, count, fp) in enumerate(tool_counts):
        for j in range(count):
            steps.append(StepRecord(
                tool=tool,
                args={"element": fp} if fp else {},
                output="ok",
                ok=True,
                loop_hint="循环检测" if (has_loop_hint and i >= 2) else "",
            ))
    return steps


def test_problem_user_pattern():
    """场景：fill_form → Continue → dismiss → 重填 → Continue → dismiss ×2 → timeout
    应该命中 front_end_bug（form_clear_pattern ≥ 2 + dismiss ≥ 2）。"""
    steps = []
    for cycle in range(3):
        steps.append(StepRecord(tool="browser_fill_form", args={}, output="ok", ok=True))
        steps.append(StepRecord(tool="browser_click", args={"element": "Continue 按钮"}, output="ok", ok=True))
        steps.append(StepRecord(tool="browser_click", args={"element": "Dismiss error 按钮"}, output="ok", ok=True))

    result = CaseResult(
        name="problem_user", url="https://saucedemo.com", instruction="test",
        status="error", error="用例超时（>900s）",
        steps=steps, duration_s=549,
        expected_fail=True, expected_fail_reason="known bug",
    )
    fc = classify(result)
    assert fc.failure_category == "front_end_bug", (
        f"应该是 front_end_bug，实 {fc.failure_category} "
        f"(matched={fc.matched_signals})"
    )
    assert fc.confidence >= 0.85, f"置信度太低 {fc.confidence}"
    print(f"[PASS] problem_user pattern → {fc.failure_category} ({fc.confidence})")


def test_agent_loop():
    """场景：browser_find 连刷 5 次相似参数，loop_hint 非空
    应该命中 agent_loop。"""
    steps = make_steps([("browser_find", 5, "Sauce Labs Backpack")], has_loop_hint=True)
    result = CaseResult(
        name="stuck_find", status="failed",
        steps=steps, duration_s=120,
    )
    fc = classify(result)
    assert fc.failure_category == "agent_loop", (
        f"应该是 agent_loop，实 {fc.failure_category}"
    )
    print(f"[PASS] agent loop → {fc.failure_category} ({fc.confidence})")


def test_timeout():
    """场景：run 到 timeout
    应该命中 agent_timeout。"""
    result = CaseResult(
        name="slow_case", status="error", error="用例超时（>300s）",
        steps=make_steps([("browser_find", 3, "")]), duration_s=301,
    )
    fc = classify(result)
    assert fc.failure_category == "agent_timeout", (
        f"应该是 agent_timeout，实 {fc.failure_category}"
    )
    print(f"[PASS] timeout → {fc.failure_category} ({fc.confidence})")


def test_passed_not_classified():
    """场景：正常 passed
    不应该走 classify（_finalize 里 result.status != 'passed' 才调），
    但即便手动调 classify 也应该返回 unknown。"""
    result = CaseResult(
        name="normal_pass", status="passed",
        steps=make_steps([("browser_find", 2, "")]), duration_s=45,
    )
    fc = classify(result)
    assert fc.failure_category == "unknown", (
        f"passed 应该返回 unknown（没信号），实 {fc.failure_category}"
    )
    print(f"[PASS] passed → {fc.failure_category}（没信号）")


def test_assertion_judge():
    """场景：agent 正常执行完但裁判说断言语义不匹配
    应该命中 assertion_judge。"""
    steps = make_steps([
        ("browser_navigate", 1, ""),
        ("browser_click", 1, "Add to cart"),
        ("browser_click", 1, "Checkout"),
    ])
    result = CaseResult(
        name="assertion_fail", status="failed",
        steps=steps, duration_s=60,
        assertions=[
            {"passed": False, "check": "断言 Checkout 成功页面", "message": "语义证据不足，无法确定"},
        ],
    )
    fc = classify(result)
    assert fc.failure_category == "assertion_judge", (
        f"应该是 assertion_judge，实 {fc.failure_category}"
    )
    print(f"[PASS] assertion_judge → {fc.failure_category} ({fc.confidence})")


def test_run_code_unsafe_loop():
    """场景：browser_run_code_unsafe 连刷很多次
    应该命中 front_end_bug（LLM 绕过后门的死循环通常是前端 bug 导致）。"""
    steps = []
    for i in range(10):
        steps.append(StepRecord(
            tool="browser_run_code_unsafe",
            args={"code": f"document.querySelector('button')"},
            output="ok", ok=True,
            loop_hint="循环检测：禁止使用 browser_run_code_unsafe",
        ))
    result = CaseResult(
        name="loop_backdoor", status="error", error="用例超时（>900s）",
        steps=steps, duration_s=901,
    )
    fc = classify(result)
    # 优先级：consecutive_run_code >= 3 → front_end_bug
    assert fc.failure_category == "front_end_bug", (
        f"run_code_unsafe 连刷应该归类 front_end_bug，实 {fc.failure_category}"
    )
    print(f"[PASS] run_code_unsafe loop → {fc.failure_category} ({fc.confidence})")


if __name__ == "__main__":
    test_problem_user_pattern()
    test_agent_loop()
    test_timeout()
    test_passed_not_classified()
    test_assertion_judge()
    test_run_code_unsafe_loop()
    print("\n=== 6/6 分类器单测全过 ===")
