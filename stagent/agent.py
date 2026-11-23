"""Agent 主循环：观察 -> 思考 -> 行动 -> 断言 -> 汇报。

设计要点：
1. 复用 MCP 暴露的 browser_* 工具，不自己造浏览器协议；
2. 工具失败时先尝试自愈定位，再把"带着修复痕迹的结果"交回给模型决策；
3. 每条用例结束后用独立的断言裁判请求判断通过与否，避免模型自说自话；
4. 断言裁判从 MCP 工具 output 里搜 Page URL 证据（navigate / click / snapshot 自动带）；
5. 给模型的 messages 里只截掉 **Snapshot** 区块（巨型、耗 token），**保留 Page 区块**
   （短、给模型当前页状态很有用）和 Events。完整 output 存 step.output 供 verify grep。
6. 裁判证据有三条通道：snapshot_history（自动快照）+ 所有步骤 output 摘要 + Page URL，
   任意一条能佐证就不该被误判为失败。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .heal_log import HealLog
from .healer import Healer
from .llm import LLM, LLMError
from .mcp_bridge import MCPBridge, ToolCallResult
from .snapshot import compact_snapshot
from .suite import Case

HEALABLE_TOOLS = {
    "browser_click", "browser_type", "browser_hover",
    "browser_select_option", "browser_drag",
}

ACTION_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_navigate_forward",
    "browser_click", "browser_type", "browser_press_key",
    "browser_select_option", "browser_handle_dialog", "browser_tabs",
}

SYSTEM_PROMPT = """你是一名资深 Web UI 自动化测试智能体（QA Agent）。你通过 MCP 工具操控一个真实浏览器，
把自然语言测试意图翻译成浏览器操作，并给出可复核的结论。

【工作循环】观察 -> 思考 -> 行动 -> 断言 -> 汇报

【硬性规则】
1. 动手前先看页面：先 browser_navigate 打开目标地址，或 browser_snapshot 获取当前快照。
2. 快照里的 [ref=e12] 是元素定位句柄。页面一旦变化，旧 ref 可能失效；做完点击/输入/跳转后，
   先重新 browser_snapshot，再用新的 ref 操作。
3. browser_click / browser_type 的 element 参数写人类可读描述（如"登录按钮"），
   ref 写快照里的句柄，两个都给，不要只给 CSS 选择器。
4. 关键步骤后调用 browser_take_screenshot 截图，filename 用 step_02_填写账号.png 这类可读名字。
5. MCP 工具的 output 里有 ### Page 区块，自动告诉你当前的 Page URL 和 Page Title——
   不要调 browser_evaluate 取 URL，读这里就行。
6. 断言只能依据工具返回的真实内容（Page URL、快照里的元素、console 报错），禁止臆测；
   看不到就当没验证成功。
7. 失败时别急着放弃：读报错，重新快照，换一条路径或换一个元素再试。
8. 不要提交真实订单、真实支付、真实删除；需要真实凭证时停下来并说明。
9. 一次只做一个明确动作，不要把多个操作塞进一个工具调用。
10. 禁止用 browser_evaluate 查 DOM 状态（querySelector、读 innerText/value/属性等）——
    该用 browser_snapshot 获取快照里的可交互元素，或 browser_find 搜索文本。
    browser_evaluate 只用于：需要注入 JS 触发浏览器事件、或 MCP 工具无法覆盖的特殊场景。
10.1 严禁使用 browser_run_code_unsafe——它能跑原生 Playwright code，是调试后门，
    会绕过 MCP 协议层和所有自愈/循环检测机制，导致行为不可复核。
    如果 fill_form / click / type 看起来失效，先用 browser_find 搜索替代元素、
    或 browser_snapshot 看当前页面真实状态，再决定下一步。
11. 收到 "No matches found" 或任何"没找到"的结果时：**先 browser_snapshot 看当前页面真实状态**，
    再决定换个搜索词、换一种验证方式（读 Page URL）、或重新理解页面。
    严禁连续多次用完全相同的参数重复 browser_find——那样纯粹浪费步数。
11.1 ⚠️ **收到 loop_hint 循环检测提示时，必须立即停止当前操作**：
    - 提示文字以 "【循环检测】" 开头，前面带 ⚠️ 符号，出现在工具 output 末尾
    - 不要忽略它继续相同的 browser_find / browser_type / browser_click
    - 正确做法：browser_snapshot 看真实页面 → 换一个完全不同的搜索词或直接跳到下一步
    - 如果 loop_hint 说的是"前端 bug"（如 Continue 后值被清掉），直接输出失败结论让裁判判定
    - 无视 loop_hint 会触发 loop_streak>=4 硬拒绝，用例被强制终止
12. 验证点击效果时，优先看 click 工具自动带的 Page URL 字段判断页面是否跳转。
    跳了就是跳转类操作成功；没跳说明点击可能被拦截或点击的元素不对。

【收尾】任务完成时不要再调用工具，直接用中文输出：
- 结论：通过 / 失败 / 部分通过
- 关键过程：逐条动作与结果
- 失败原因：具体证据（报错文本、页面实际文案、Page URL）
- 建议：下一步需要人工确认什么"""

VERIFY_SYSTEM = """你是严格的测试断言裁判。只根据给出的页面快照与用例意图判断每条断言是否成立。

只输出 JSON，不要解释文字：
{"items": [{"index": 0, "assertion": "原文", "passed": true, "evidence": "支撑判断的页面原文/URL"}]}

规则：
- passed 为 true 必须有明确证据；证据不足一律 false。
- URL / 跳转类断言：搜 "Page URL: ..." 这一行，它是页面当前 URL 的权威来源；
  不要猜测"应该跳转了"，必须有实际 URL 文本。
- 快照按时间顺序编号，断言里说"登录前"时用前面的快照，"登录后"时用后面的快照。
- **工具 output 摘要里的 browser_find 匹配结果也是合法证据**——如果某个 browser_find
  找到了目标文本/元素，说明那一刻页面上真实存在它。
- 页面里看不到的内容不要假设它存在。
- 输出条目顺序与输入断言顺序一致。"""

# 从 MCP 工具 output 里抽 Page URL 行（给 verify 用）
_URL_IN_PAGE_RE = re.compile(r"^\s*-\s*Page URL:\s*(https?://\S+)", re.MULTILINE)

# 给模型的 messages 里只截断 Snapshot 区块（巨型 YAML，占大量 token）
# Page 区块保留（短、有 URL/Title 给模型当前页状态很有用）
_BLOCKS_TO_STRIP_RE = re.compile(
    r"^\s*###\s+Snapshot\b.*?(?=^\s*###\s|\Z)",
    re.MULTILINE | re.DOTALL,
)

# ---------- 循环检测辅助（模块级，便于离线单测） ----------

_RANGE_NUM_RE = re.compile(r"\{\s*\d+\s*,\s*\d*\s*\}")


def find_args_similar(a: dict, b: dict) -> bool:
    """两次 browser_find 的参数是否"本质相同"：regex/text 归一化（数字范围统一成 {0,N}）后相等。

    覆盖模式：Backpack[\\s\\S]{0,500} 与 Backpack[\\s\\S]{0,20000000000} 视为同一次查找。
    """
    a_txt = str(a.get("regex") or a.get("text") or "").lower().strip()
    b_txt = str(b.get("regex") or b.get("text") or "").lower().strip()
    if not a_txt or not b_txt:
        return False
    a_norm = _RANGE_NUM_RE.sub("{0,N}", a_txt)
    b_norm = _RANGE_NUM_RE.sub("{0,N}", b_txt)
    return a_norm == b_norm


def find_output_similar(a: str, b: str) -> bool:
    """两次 browser_find 的结果是否"同类"：同是 No matches found 或同是有匹配。"""
    a_empty = "No matches found" in (a or "")
    b_empty = "No matches found" in (b or "")
    return a_empty == b_empty


@dataclass
class StepRecord:
    index: int = 0
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    output: str = ""
    duration_s: float = 0.0
    healed: bool = False
    heal_notes: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    error: str = ""
    loop_hint: str = ""  # 循环检测提示——通过 _render_tool_output 渲染进模型上下文


@dataclass
class CaseResult:
    name: str = ""
    url: str = ""
    instruction: str = ""
    status: str = "completed"
    answer: str = ""
    steps: list[StepRecord] = field(default_factory=list)
    healings: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0
    error: str = ""
    expected_fail: bool = False      # 该 case fail 是否为预期（来自 suite Case.expected_fail）
    expected_fail_reason: str = ""
    # 失败分类（failure_classifier.py 产出，passed 时为 None）
    failure_category: str = ""
    failure_confidence: float = 0.0
    failure_reason: str = ""


class TestAgent:
    def __init__(self, cfg: Config, bridge: MCPBridge, llm: LLM):
        self.cfg = cfg
        self.bridge = bridge
        self.llm = llm
        self.heal_log = HealLog()
        self.healer = Healer(bridge, llm, cfg.agent, self.heal_log)
        # 默认把"后门工具"从 LLM 可见列表里隐藏——
        # browser_run_code_unsafe 能跑原生 Playwright code，绕过 MCP 协议层；
        # 一旦 LLM 发现就会一直用，再也不用 fill_form/type/click 了（见 problem_user 第二次跑 17+11 次连刷）。
        # 框架自己仍可通过 bridge.call 调它。
        _HIDDEN_FROM_LLM = {"browser_run_code_unsafe"}
        all_tools = bridge.openai_tools()
        self._tools = [t for t in all_tools if (t.get("function") or {}).get("name") not in _HIDDEN_FROM_LLM]
        self._tool_names = set(bridge.tool_names)  # 完整集合（含隐藏的），_run_tool 要查表
        self.steps: list[StepRecord] = []
        self.screenshots: list[str] = []
        self._snapshot_history: list[str] = []
        self._loop_streak: int = 0  # 相似操作计数（硬拒绝用）
        self._loop_window: list[tuple[str, str]] = []  # 最近 8 步的 loop 工具指纹（跨工具不清零）

    async def run_case(self, case: Case) -> CaseResult:
        started = time.time()
        self.steps = []
        self.screenshots = []
        self._snapshot_history = []
        self._loop_streak = 0
        self._loop_window = []
        self.heal_log.reset()
        result = CaseResult(
            name=case.name, url=case.url, instruction=case.instruction,
            expected_fail=case.expected_fail, expected_fail_reason=case.expected_fail_reason,
        )

        try:
            if case.url:
                nav, _ = await self._run_tool("browser_navigate", {"url": case.url})
                if nav.is_error:
                    result.status = "error"
                    result.error = f"打开 {case.url} 失败：{nav.text[:300]}"
                    return self._finalize(result, started)

            # Case 级 max_steps 覆盖全局 config（0 表示用默认）
            effective_max_steps = max(1, case.max_steps or self.cfg.agent.max_steps)
            answer = await self.run_instruction(
                case.instruction, case_name=case.name, start_url=case.url,
                max_steps=effective_max_steps,
            )
            result.answer = answer

            if case.assertions:
                # 通道 1：snapshot_history 里的 compact snapshot（优先）
                recent_snaps = self._snapshot_history[-5:] if self._snapshot_history else []
                snap_text = "\n\n".join(
                    f"【快照 #{i}】\n{compact_snapshot(s, limit=2000)}"
                    for i, s in enumerate(recent_snaps, start=1)
                )
                all_outputs = "\n".join(s.output for s in self.steps if s.output)

                # 通道 2：Page URL 证据（导航类断言的硬证据）
                url_lines = _URL_IN_PAGE_RE.findall(all_outputs)
                url_evidence = "\n".join(
                    f"  - Page URL（步骤 #{i}）: {u}"
                    for i, u in enumerate(url_lines, start=1)
                )
                if url_evidence:
                    snap_text += f"\n\n【Page URL 证据（按时间顺序，来自 MCP 工具 output）】\n{url_evidence}"

                # 通道 3：所有步骤 output 的原文摘要（兜底！browser_find 匹配结果、
                # browser_fill_form 执行结果、click 后的自动 Snapshot 片段都在这里）
                step_excerpts = []
                for i, s in enumerate(self.steps, start=1):
                    if not s.output:
                        continue
                    excerpt = s.output[:1200].strip()
                    if excerpt:
                        step_excerpts.append(f"【步骤 #{i} {s.tool}】\n{excerpt}")
                if step_excerpts:
                    snap_text += "\n\n【各步骤工具 output 原文摘要（含匹配结果/元素树等）】\n" + "\n\n".join(step_excerpts)

                if not snap_text.strip():
                    snap_text = "(执行过程中没有任何工具输出)"
                verdict = await self.verify(case.assertions, case.instruction, snap_text)
                result.assertions = verdict
                if not verdict:
                    result.status = "completed"
                elif all(item.get("passed") for item in verdict):
                    result.status = "passed"
                else:
                    result.status = "failed"
            else:
                result.status = "completed"
        except LLMError as exc:
            result.status = "error"
            result.error = str(exc)
        except Exception as exc:
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"

        return self._finalize(result, started)

    async def run_instruction(
        self,
        instruction: str,
        case_name: str = "",
        start_url: str = "",
        max_steps: int | None = None,
        _continuation_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        # Phase 2：续命时复用已有 messages 上下文，不重建对话历史
        if _continuation_messages is not None:
            messages = _continuation_messages
        else:
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._user_message(instruction, case_name, start_url)},
        ]
        answer = ""
        # 优先用传入的 Case 级值，否则回退到全局 config 默认 40
        max_steps = max(1, max_steps or self.cfg.agent.max_steps)

        for index in range(1, max_steps + 1):
            # ★ 硬拒绝兜底：软提示失效时（模型无视 loop_hint 继续刷同一类工具），
            # streak>=4 直接终止，避免烧光 max_steps / timeout。
            # 覆盖：find / evaluate / run_code_unsafe / type / press_key 五种。
            if self._loop_streak >= 4:
                # ★ Phase 2 续命：loop_streak 杀停但 agent 还在跑（不是 0 步就崩），
                # 给一次机会扩 max_steps + 清 streak，让 agent 换策略继续。
                # 只有 1 次续命机会（已经达到 max_steps 续命 2 次后再来就是真循环）
                loop_relief_rounds = getattr(self, "_loop_relief_rounds", 0)
                if loop_relief_rounds < 1 and index >= 5:
                    self._loop_relief_rounds = loop_relief_rounds + 1
                    self._loop_streak = 0  # 清 streak
                    relief = 10
                    print(
                        f"  ⚡ [LOOP-RELIEF] loop_streak>=4 杀停（已跑 {index} 步），"
                        f"清 streak + 扩 max_steps +{relief} 让 agent 换策略（第 {self._loop_relief_rounds} 次）"
                    )
                    # 扩预算 + 清 streak → continue 回到 for 循环继续跑
                    max_steps += relief
                    continue

                return (
                    f"【循环终止】连续 {self._loop_streak} 次用相似的工具参数重复相同操作而没有推进任务，"
                    f"执行器强制结束本用例。已完成的操作见步骤记录；"
                    f"断言将由独立的裁判依据已有证据（Page URL、快照、工具 output 摘要）判定。"
                )

            message = await self.llm.chat(messages, tools=self._tools)
            content = (message.get("content") or "").strip()
            calls = message.get("tool_calls") or []

            if not calls:
                answer = content or answer
                messages.append({"role": "assistant", "content": content})
                return answer or "模型结束了任务但没有给出结论。"

            messages.append(message)
            for call in calls:
                result, step = await self._execute_call(call, index)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": self._render_tool_output(result, step),
                    }
                )

        # ★ Phase 2：max_steps 杀停后自动续命——检测"差几步就完了"信号
        # 触发条件：循环结束但 answer 为空 + 最后 5 步里有 >=3 步 ok=True 且非 loop_hint
        # 说明 agent 在 CI 慢环境下一直在推进，只是步数预算不够。
        relief_rounds = getattr(self, "_relief_rounds", 0)
        last_ok_count = sum(1 for s in self.steps[-5:] if s.ok and not s.loop_hint)
        if relief_rounds < 2 and last_ok_count >= 3 and not answer:
            self._relief_rounds = relief_rounds + 1
            relief = 15  # 每次续 15 步
            print(
                f"  ⚡ [RELIEF] max_steps={max_steps} 杀停但最后 5 步有 {last_ok_count} 步在推进，"
                f"自动扩 {relief} 步（第 {self._relief_rounds} 次续命）"
            )
            # 扩预算重走循环——for 循环已结束，用递归调用
            return await self.run_instruction(
                instruction, case_name=case_name, start_url=start_url,
                max_steps=max_steps + relief,
                _continuation_messages=messages,
            )

        return answer or f"达到最大步数（{max_steps}）仍未见结论，任务可能未完成。"

    async def verify(
        self,
        assertions: list[str],
        instruction: str,
        evidence_text: str,
    ) -> list[dict[str, Any]]:
        if not assertions:
            return []
        listing = "\n".join(f"{i}. {a}" for i, a in enumerate(assertions))
        user = (
            f"【用例意图】\n{instruction}\n\n"
            f"【待判定断言】\n{listing}\n\n"
            f"【证据（页面快照历史 + Page URL + 各步骤 output 原文摘要）】\n{evidence_text}\n"
        )
        try:
            data = await self.llm.chat_json(VERIFY_SYSTEM, user)
        except (LLMError, ValueError):
            return []

        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            return []

        by_index: dict[int, dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            by_index[idx] = item

        verdict: list[dict[str, Any]] = []
        for i, assertion in enumerate(assertions):
            item = by_index.get(i, {})
            verdict.append(
                {
                    "assertion": assertion,
                    "passed": bool(item.get("passed")),
                    "evidence": str(item.get("evidence") or "未给出证据"),
                }
            )
        return verdict

    async def _execute_call(self, call: dict[str, Any], index: int):
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            if not isinstance(args, dict):
                raise ValueError("arguments 必须是 JSON 对象")
        except Exception:
            text = f"工具参数不是合法 JSON 对象：{str(raw_args)[:200]}"
            record = StepRecord(
                index=index, tool=name or "unknown", args={}, ok=False,
                output=text, duration_s=0.0, error=text,
            )
            self.steps.append(record)
            return ToolCallResult(text=text, is_error=True), record

        return await self._run_tool(name, args, index=index)

    async def _run_tool(
        self,
        name: str,
        args: dict[str, Any],
        index: int | None = None,
    ) -> tuple[ToolCallResult, StepRecord]:
        if index is None:
            index = len(self.steps) + 1

        if name not in self._tool_names:
            available = ", ".join(sorted(self._tool_names))
            text = f"未知工具 {name}。可用工具：{available}"
            record = StepRecord(index, name, args, False, text, 0.0, error=text)
            self.steps.append(record)
            return ToolCallResult(text=text, is_error=True), record

        started = time.time()
        current = dict(args)
        result = await self.bridge.call(name, current)
        heal_notes: list[str] = []

        if result.is_error and name in HEALABLE_TOOLS:
            for attempt in range(1, max(1, self.cfg.agent.heal_attempts) + 1):
                proposal = await self.healer.propose(name, current, result.text, attempt)
                if proposal is None:
                    break
                current, meta = proposal
                heal_notes.append(
                    f"第{attempt}次自愈：{meta.get('reason') or '重新定位'}（ref={meta.get('ref')}）"
                )
                result = await self.bridge.call(name, current)
                if not result.is_error:
                    break

        # ── 策略级重试：browser_fill_form 返回成功但 DOM 值没真写进去
        #    （problem_user / error_user 等 bug 账号的 Checkout 表单 bug）
        #    尝试框架级自动切换到 browser_type 逐字输入，LLM 不需要做任何事
        if (
            not result.is_error
            and name == "browser_fill_form"
            and isinstance(args.get("fields"), list)
        ):
            retry_triggered, retry_result = await self._strategy_retry_fill_form_to_type(args)
            if retry_triggered:
                # 替换结果：append 一条 hint 到 text 让模型知道值已经被框架补上
                extra = (
                    "\\n\\n【框架策略重试】检测到 browser_fill_form 成功但 DOM 值未写入——"
                    "已自动用 browser_type 逐字输入重新填入，值应已生效。"
                )
                result = ToolCallResult(
                    text=(result.text or "") + extra,
                    is_error=bool(retry_result and retry_result.get("is_error")),
                )
                heal_notes.append("框架策略重试：fill_form→type（表单值未写入 DOM）")

        if name == "browser_snapshot" and result.text and not result.is_error:
            self._snapshot_history.append(result.text)

        if (
            self.cfg.agent.auto_observe
            and not result.is_error
            and name in ACTION_TOOLS
        ):
            snap = await self.bridge.call("browser_snapshot", {})
            if not snap.is_error and snap.text:
                self._snapshot_history.append(snap.text)
                result = ToolCallResult(
                    text=(result.text or "") + "\n\n【当前页面可交互元素】\n" + compact_snapshot(snap.text),
                    is_error=False,
                    images=list(result.images),
                )

        if (
            self.cfg.agent.screenshot_each_step
            and not result.is_error
            and name in ACTION_TOOLS
            and name != "browser_take_screenshot"
        ):
            shot_name = f"step_{index:02d}_{name.replace('browser_', '')}.png"
            shot = await self.bridge.call("browser_take_screenshot", {"filename": shot_name})
            if not shot.is_error:
                result = ToolCallResult(
                    text=(result.text or "") + f"\n[已自动截图：{shot_name}]",
                    is_error=result.is_error,
                    images=list(result.images) + list(shot.images),
                )

        record = StepRecord(
            index=index,
            tool=name,
            args=current,
            ok=not result.is_error,
            output=(result.text or "")[:2000],
            duration_s=round(time.time() - started, 2),
            healed=bool(heal_notes),
            heal_notes=heal_notes,
            screenshots=[str(p) for p in result.images],
            error="" if not result.is_error else (result.text or "")[:800],
        )

        # ★ 防无限循环（滑动窗口版）：
        #   维护一个 _loop_window（最近 8 步 loop 工具的指纹），
        #   新 step 的指纹如果在窗口里已出现过 → streak++（不清零！跨工具累加），
        #   如果没出现 → 只进窗口，不清零 streak。
        #   这样能检测到 find→click→type→find→click→type 这类"相同动作隔几步重复"模式。
        #   覆盖 5 种工具：find / evaluate / run_code_unsafe / type / press_key。
        _LOOP_TOOLS = {"browser_find", "browser_evaluate", "browser_run_code_unsafe",
                       "browser_type", "browser_press_key"}
        if name in _LOOP_TOOLS:
            def _fingerprint(tool: str, a: dict) -> str:
                import re as _re
                if tool == "browser_find":
                    # find 用 args 序列化（target/similarity 可忽略，因为 find_args_similar 已经排除）
                    import json as _json
                    args_copy = {k: v for k, v in a.items() if k not in ("target", "similarity")}
                    return f"find|{_json.dumps(args_copy, sort_keys=True, ensure_ascii=False)}"
                if tool in ("browser_evaluate", "browser_run_code_unsafe"):
                    for k in ("function", "expression", "script", "code"):
                        v = str(a.get(k) or "")
                        if v:
                            v = _re.sub(r"\s+", " ", v).strip()
                            return f"{tool}|{v[:80]}"
                if tool == "browser_type":
                    return f"type|{a.get('element') or a.get('target') or ''}|{a.get('text') or ''}"
                if tool == "browser_press_key":
                    return f"press|{a.get('key') or ''}"
                return ""

            fp = _fingerprint(name, current)
            if fp:
                # 窗口里已有相同指纹 → streak++（不清零）
                dup_count = sum(1 for _, existing_fp in self._loop_window if existing_fp == fp)
                if dup_count > 0:
                    self._loop_streak += 1
                # 新指纹入窗口，窗口上限 8
                self._loop_window.append((name, fp))
                if len(self._loop_window) > 8:
                    self._loop_window.pop(0)

                # 不管 streak 清不清零，只要 repeat_count>=2（窗口里已出现过）就给 loop_hint
                if dup_count >= 1:
                    repeat_count = dup_count + 1
                    if name == "browser_find":
                        if "No matches found" in (result.text or ""):
                            record.loop_hint = (
                                f"【循环检测】最近 8 步里你已第 {repeat_count} 次用相似的 browser_find 参数查找，全部 No matches found。"
                                "立即停止重复查找：先 browser_snapshot 看当前页面真实状态，"
                                "换一个搜索词，或改用 Page URL 证据下结论。"
                            )
                        else:
                            record.loop_hint = (
                                f"【循环检测】最近 8 步里你已第 {repeat_count} 次用相似的 browser_find 参数查找，每次都成功匹配但都没有采取下一步动作。"
                                "立即停止重复查找：需要确认状态就用 browser_snapshot，"
                                "需要推进流程就直接执行下一步操作（如 browser_click 进入购物车）。"
                            )
                    elif name == "browser_evaluate":
                        record.loop_hint = (
                            f"【循环检测】最近 8 步里你已第 {repeat_count} 次用相似的 browser_evaluate 执行同一段 JS。"
                            "System Prompt 禁止用 browser_evaluate 查 DOM 状态——"
                            "你应该用 browser_snapshot 看页面，或直接执行下一步操作（browser_click / browser_type）推进流程。"
                        )
                    elif name == "browser_run_code_unsafe":
                        record.loop_hint = (
                            f"【循环检测】最近 8 步里你已第 {repeat_count} 次用相似的 browser_run_code_unsafe 执行原生 Playwright code。"
                            "System Prompt 严禁使用 browser_run_code_unsafe——"
                            "它是调试后门，会绕过 MCP 协议层。请改用 browser_fill_form / browser_type / browser_click。"
                        )
                    elif name == "browser_type":
                        record.loop_hint = (
                            f"【循环检测】最近 8 步里你已第 {repeat_count} 次对同一元素做相同的 browser_type 输入。"
                            "如果 Continue 之后值又被清掉，说明这是前端 bug（已知），"
                            "不要再重填——直接输出失败结论，让裁判判定。"
                        )
                    else:  # browser_press_key
                        record.loop_hint = (
                            f"【循环检测】最近 8 步里你已第 {repeat_count} 次重复相同的 browser_press_key 按键序列。"
                            "页面可能有前端 bug 阻止表单提交——停止重试，直接输出结论让裁判判定。"
                        )

        self.steps.append(record)
        for image in result.images:
            self.screenshots.append(str(image))
        return result, record

    # ------------------------------------------------------------ 辅助
    async def _strategy_retry_fill_form_to_type(self, fill_form_args: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        """框架级策略重试：browser_fill_form 成功但 DOM 值未写入 → 自动逐字段 browser_type。

        返回 (是否触发了重试, 最后一次 type 的结果 dict)。不改变调用方的 flow——
        调用方把 retry_result 忽略也没关系。

        触发条件：fill_form 返回成功 → 用 browser_evaluate 逐字段读 value → 发现有字段值没写进去。
        如果所有字段都写进去了，返回 (False, None)。
        """
        fields = fill_form_args.get("fields") or []
        if not isinstance(fields, list) or not fields:
            return False, None

        # 第一步：evaluate 读每个字段的实际 DOM value
        # evaluate 参数：{"expression": "() => { ... }"}  用 JS 一次性读出所有 ref 的 value
        # MCP browser_evaluate 不支持变量，只能硬编码。用一个循环多次 evaluate。
        refs_to_check: list[tuple[str, str]] = []  # [(ref, expected_value), ...]
        for f in fields:
            if not isinstance(f, dict):
                continue
            ref = str(f.get("target") or "")
            val = str(f.get("value") or "")
            if ref and val and f.get("type") in ("textbox", "textarea", "number", "search", "tel", "url", "email", "password"):
                refs_to_check.append((ref, val))

        if not refs_to_check:
            return False, None

        # evaluate 格式：不同版本 MCP 可能不同——先试 function 再试 expression
        async def _eval(expr: str) -> str:
            for kw in ("function", "expression", "script"):
                r = await self.bridge.call("browser_evaluate", {kw: expr})
                if not r.is_error:
                    return str(r.text or "")
            return ""

        dirty_refs: set[str] = set()
        for ref, expected in refs_to_check:
            # MCP 的 ref 是数据属性，evaluate 用 document.querySelector 读
            js = (
                f"() => {{ "
                f"const el = document.querySelector('[data-test=\"{ref}\"]') || "
                f"document.querySelector('[ref=\"{ref}\"]') || "
                f"document.querySelector('{ref}'); "
                f"return el ? (el.value ?? '') : ''; }}"
            )
            actual = await _eval(js)
            if actual.strip() != expected.strip():
                dirty_refs.add(ref)

        if not dirty_refs:
            return False, None  # 所有值都写进去了，不需要重试

        # 第二步：对 dirty_refs 里的字段逐个 browser_type
        # 先 browser_click 聚焦 → 再 browser_type 逐字输入（clear + type）
        last_result: dict[str, Any] | None = None
        for f in fields:
            if not isinstance(f, dict):
                continue
            ref = str(f.get("target") or "")
            if ref not in dirty_refs:
                continue
            name = str(f.get("name") or ref)
            val = str(f.get("value") or "")
            # click 聚焦
            await self.bridge.call("browser_click", {"element": name, "target": ref})
            # type 逐字输入（clear=true 先清旧值）
            type_args = {"element": name, "target": ref, "text": val, "clear": True}
            r = await self.bridge.call("browser_type", type_args)
            last_result = {"is_error": bool(r.is_error), "text": str(r.text or "")}

        return True, last_result

    def _render_tool_output(self, result: ToolCallResult, step: StepRecord) -> str:
        """给模型的 messages 里只截断 Snapshot 区块。完整 output 存 step.output。"""
        full_text = result.text or ("（工具没有返回文本）" if not result.is_error else "（工具失败且无输出）")
        if step.healed:
            full_text += "\n[提示：本次操作触发了自愈定位，" + "；".join(step.heal_notes) + "]"
        if step.loop_hint:
            # 循环检测提示——必须渲染进模型上下文，否则模型永远看不到
            full_text += "\n\n⚠️ " + step.loop_hint
        # 只删 Snapshot 区块（巨型 YAML），保留 Page（有 URL/Title）和 Events
        compact = _BLOCKS_TO_STRIP_RE.sub("", full_text)
        limit = max(500, self.cfg.agent.tool_result_char_limit)
        if len(compact) > limit:
            # 截断时把 loop_hint 拼回末尾，保证提示不被截掉
            compact = compact[:limit] + f"\n...[输出过长，已截断 {len(compact) - limit} 字符]"
            if step.loop_hint:
                compact += "\n\n⚠️ " + step.loop_hint
        return compact

    def _user_message(self, instruction: str, case_name: str, start_url: str) -> str:
        parts: list[str] = []
        if case_name:
            parts.append(f"【用例】{case_name}")
        if start_url:
            parts.append(f"【起始地址】{start_url}")
        parts.append("【测试意图】\n" + instruction.strip())
        parts.append(
            "【执行要求】\n"
            "1. 先观察页面再操作；\n"
            "2. 每完成一个关键动作截图一次；\n"
            "3. 结束时给出中文结论（通过/失败/部分通过 + 证据）。"
        )
        return "\n\n".join(parts)

    def _finalize(self, result: CaseResult, started: float) -> CaseResult:
        result.steps = list(self.steps)
        result.screenshots = list(self.screenshots)
        result.healings = self.heal_log.as_dicts()
        result.duration_s = round(time.time() - started, 2)

        # ★ Phase 2.1 失败分类：passed 不分类，只对非 passed 跑分类器
        if result.status != "passed":
            try:
                from .failure_classifier import classify
                fc = classify(result)
                result.failure_category = fc.failure_category
                result.failure_confidence = fc.confidence
                result.failure_reason = fc.reason
            except Exception:
                # 分类器故障不阻塞主流程
                pass
        return result
