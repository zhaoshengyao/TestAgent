"""命令行入口。

  python -m stagent doctor
  python -m stagent run suites/smoke_login.yaml
  python -m stagent ask "打开 saucedemo 用演示账号登录，并截图"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from .agent import CaseResult, TestAgent
from .config import Config, load_config
from .doctor import run_doctor
from .llm import LLM
from .mcp_bridge import MCPBridge
from .reporter import write_reports
from .suite import Case, load_suite


def _default_out_dir(cfg: Config) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return cfg.base_dir / "artifacts" / "runs" / stamp


def _meta(cfg: Config) -> dict:
    return {
        "provider": cfg.llm.provider,
        "model": cfg.llm.model,
        "base_url": cfg.llm.base_url,
        "headless": "无头" if cfg.browser.headless else "有头",
        "heal_attempts": cfg.agent.heal_attempts,
        "max_steps": cfg.agent.max_steps,
    }


def _exit_code_from_results(results: list) -> int:
    """exit code 三态判定：

    0 = 全 passed
    2 = 有 expected_fail 但无意外（expected_fail 的 case 无论 status 是 failed 还是 error 都算预期）
    1 = 有 unexpected_fail 或 非expected_fail 的 error

    Phase 2 改动：expected_fail 的 case 如果跑崩（error），算 expected_fail 不算红线。
    因为声明 expected_fail 就是探测 bug 的——崩了说明 bug 表现更严重而已。
    """
    unexpected_fail = sum(
        1 for r in results
        if not getattr(r, "expected_fail", False)
        and r.status in ("failed", "completed")
    )
    # 只统计非 expected_fail 的 error
    err = sum(
        1 for r in results
        if r.status == "error" and not getattr(r, "expected_fail", False)
    )
    if unexpected_fail or err:
        return 1
    expected_fail = sum(
        1 for r in results
        if getattr(r, "expected_fail", False)
        and r.status in ("failed", "completed", "error")
    )
    return 2 if expected_fail else 0


async def _execute(
    cfg: Config,
    cases: list[Case],
    title: str,
    out_dir: Path,
) -> int:
    results: list[CaseResult] = []

    for i, case in enumerate(cases, start=1):
        print(f"\n[{i}/{len(cases)}] {case.name}", flush=True)

        # ★ 每个 case 独立 MCP bridge + 独立浏览器 session
        # 之前所有 case 共享同一个 bridge，Case 1 搞坏 session 会污染 Case 2
        bridge = MCPBridge(cfg.browser, cfg.base_dir, cfg.screens_dir)
        try:
            await bridge.start()
        except Exception as exc:
            print(f"✘ MCP 启动失败：{exc}", flush=True)
            results.append(CaseResult(
                name=case.name, url=case.url, instruction=case.instruction,
                status="error", error=f"MCP 启动失败：{exc}",
            ))
            continue

        tools = bridge.tool_names
        if i == 1:
            print(f"→ MCP 已就绪，暴露 {len(tools)} 个工具：{', '.join(tools[:8])} …", flush=True)
            print(f"→ 模型：{cfg.llm.model} @ {cfg.llm.base_url}", flush=True)

        agent = TestAgent(cfg, bridge, LLM(cfg.llm))
        try:
            if case.timeout_s and case.timeout_s > 0:
                try:
                    result = await asyncio.wait_for(agent.run_case(case), timeout=case.timeout_s)
                except asyncio.TimeoutError:
                    result = CaseResult(
                        name=case.name,
                        url=case.url,
                        instruction=case.instruction,
                        status="error",
                        error=f"用例超时（>{case.timeout_s:.0f}s）",
                        steps=list(agent.steps),
                        healings=agent.heal_log.as_dicts(),
                        screenshots=list(agent.screenshots),
                    )
            else:
                result = await agent.run_case(case)
        finally:
            await bridge.close()

        results.append(result)
        # 三标记：passed✅ / expected_fail且失败→⚠️ / 其它失败→❌ / error→💥
        if result.status == "passed":
            mark = "✅"
        elif result.status == "error":
            mark = "💥"
        elif result.expected_fail and result.status in ("failed", "completed"):
            mark = "⚠️"
        elif result.status == "failed":
            mark = "❌"
        else:
            mark = "⬜"
        extra = f"（预期失败：{result.expected_fail_reason}）" if (mark == "⚠️" and result.expected_fail_reason) else ""
        print(f"    {mark} {result.status}（{result.duration_s}s，{len(result.steps)} 步）{extra}", flush=True)

    paths = write_reports(out_dir, title, results, _meta(cfg))
    passed = sum(1 for r in results if r.status == "passed")
    expected_failed = sum(
        1 for r in results
        if r.expected_fail and r.status in ("failed", "completed", "error")
    )
    unexpected_failed = sum(
        1 for r in results
        if not r.expected_fail and r.status in ("failed", "completed")
    )
    errored = sum(1 for r in results if r.status == "error" and not r.expected_fail)

    print("\n" + "=" * 60)
    total_bad = unexpected_failed + errored
    if expected_failed:
        print(f"用例 {len(results)} 条：通过 {passed}，❌意外失败 {unexpected_failed}，⚠️预期失败 {expected_failed}，执行异常 {errored}")
    else:
        print(f"用例 {len(results)} 条：通过 {passed}，断言未通过 {unexpected_failed}，执行异常 {errored}")
    print(f"报告：{paths['markdown']}")
    print(f"HTML：{paths['html']}")
    print(f"数据：{paths['json']}")
    print("=" * 60)
    # exit code 三态：0=全过；2=有预期失败但无意外；1=有意外失败或异常
    return _exit_code_from_results(results)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stagent",
        description="自然语言驱动的 Web UI 回归测试智能体（Playwright MCP + 本地 LLM）",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_doctor = sub.add_parser("doctor", help="检查 Node / Ollama / 浏览器是否就绪")
    p_doctor.add_argument("--config", default=None)

    p_run = sub.add_parser("run", help="执行用例集 YAML")
    p_run.add_argument("suite", help="用例文件路径，例如 suites/smoke_login.yaml")
    p_run.add_argument("--config", default=None)
    p_run.add_argument("--out", default=None, help="报告输出目录")
    p_run.add_argument("--only", default=None, help="只跑名称/指令包含该关键词的用例")

    p_ask = sub.add_parser("ask", help="一句话直接跑")
    p_ask.add_argument("instruction", help="自然语言测试意图")
    p_ask.add_argument("--url", default="", help="起始地址")
    p_ask.add_argument("--name", default="一句话任务")
    p_ask.add_argument("--assert", dest="assertions", action="append", default=None)
    p_ask.add_argument("--config", default=None)
    p_ask.add_argument("--out", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.cmd:
        parser.print_help()
        return 1

    if args.cmd == "doctor":
        return run_doctor(args.config)

    cfg = load_config(args.config)
    if not cfg.config_path:
        print("⚠️  未找到 config.yaml，使用内置默认（Ollama qwen3:latest）。"
              "可复制 config.example.yaml 后修改。\n", flush=True)

    out_dir = Path(args.out).resolve() if args.out else _default_out_dir(cfg)

    if args.cmd == "run":
        suite = load_suite(args.suite, only=args.only)
        print(f"→ 用例集：{suite.name}（{len(suite.cases)} 条）", flush=True)
        try:
            return asyncio.run(_execute(cfg, suite.cases, suite.name, out_dir))
        except KeyboardInterrupt:
            print("\n已中断。", flush=True)
            return 130

    if args.cmd == "ask":
        case = Case(
            name=args.name,
            instruction=args.instruction,
            url=args.url,
            assertions=list(args.assertions or []),
        )
        try:
            return asyncio.run(_execute(cfg, [case], case.name, out_dir))
        except KeyboardInterrupt:
            print("\n已中断。", flush=True)
            return 130

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
