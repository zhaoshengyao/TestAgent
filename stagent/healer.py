"""自愈定位：选择器/ref 失效时，让模型从最新可访问性树里重新挑一个。

对应文章第四节的做法：
失败检测 -> 上下文采集（重新快照） -> LLM 从可交互元素中找替代 -> 用新定位重试

注意：自愈只替换"怎么定位"，不替换"断言什么"。断言失败依然是失败。
"""
from __future__ import annotations

from typing import Any

from .config import AgentConfig
from .heal_log import HealEvent, HealLog
from .llm import LLM, LLMError
from .mcp_bridge import MCPBridge
from .snapshot import format_elements, interactive_elements, parse_snapshot

_SYSTEM = """你是 Web UI 自动化测试的定位修复专家。
给定：一个失效的操作（例如"点击登录按钮"）、原始报错、当前页面的可交互元素清单。
请你从清单里挑出与原始意图最匹配的那一个元素。

只输出 JSON，不要任何解释文字，格式：
{"ref": "元素清单里的 ref，例如 e12", "element": "该元素的人类可读描述", "reason": "一句话说明为什么是它", "confidence": 0.0}

规则：
- ref 必须原样抄自元素清单里的 [ref=...]，禁止编造。
- 如果清单里确实没有匹配元素，输出 {"ref": null, "element": "", "reason": "原因", "confidence": 0.0}。
"""

# 哪些工具只靠 ref 定位，自愈时可以清掉旧的 target/CSS 选择器
# （browser_fill_form 有 fields 列表结构，不自愈改 target 的逻辑）
_REF_ONLY_TOOLS = {"browser_click", "browser_type", "browser_hover",
                    "browser_select_option", "browser_drag"}


class Healer:
    def __init__(self, bridge: MCPBridge, llm: LLM, cfg: AgentConfig, heal_log: HealLog):
        self.bridge = bridge
        self.llm = llm
        self.cfg = cfg
        self.heal_log = heal_log

    async def propose(
        self,
        tool_name: str,
        args: dict[str, Any],
        error_text: str,
        attempt: int,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        snapshot = await self.bridge.call("browser_snapshot", {})
        if snapshot.is_error:
            self.heal_log.add(
                HealEvent(
                    tool=tool_name, attempt=attempt, ok=False,
                    reason=f"重新快照失败：{snapshot.text[:200]}",
                )
            )
            return None

        elements = interactive_elements(parse_snapshot(snapshot.text))
        if not elements:
            self.heal_log.add(
                HealEvent(
                    tool=tool_name, attempt=attempt, ok=False,
                    reason="快照里没有可交互元素",
                )
            )
            return None

        candidates = format_elements(elements, limit=120)
        intent = args.get("element") or args.get("ref") or ""
        user = (
            f"【失效的操作】{tool_name}\n"
            f"【当时的元素描述】{intent}\n"
            f"【工具参数】{args}\n"
            f"【原始报错】\n{(error_text or '')[:1200]}\n\n"
            f"【当前页面可交互元素】\n{candidates}\n"
        )

        try:
            data = await self.llm.chat_json(_SYSTEM, user)
        except (LLMError, ValueError) as exc:
            self.heal_log.add(
                HealEvent(
                    tool=tool_name, attempt=attempt, ok=False,
                    reason=f"自愈推理失败：{exc}",
                )
            )
            return None

        ref = str(data.get("ref") or "").strip()
        valid_refs = {e.ref for e in elements}
        if not ref or ref not in valid_refs:
            self.heal_log.add(
                HealEvent(
                    tool=tool_name, attempt=attempt, ok=False,
                    reason=f"模型给出的 ref 无效（{ref!r}）：{data.get('reason', '')}",
                )
            )
            return None

        new_args = dict(args)
        new_args["ref"] = ref

        # ★ 关键：如果工具只靠 ref 定位，把旧的 target（可能是错的 CSS 选择器）清掉，
        # 否则 MCP 会同时用错误 target + 正确 ref 去定位，ref 就白换了。
        if tool_name in _REF_ONLY_TOOLS:
            # browser_click/browser_type 的 target 是 CSS 选择器，自愈用 ref 定位
            new_args.pop("target", None)

        element = str(data.get("element") or "").strip()
        if element:
            new_args["element"] = element

        meta = {
            "tool": tool_name,
            "attempt": attempt,
            "ok": True,
            "ref": ref,
            "element": element,
            "reason": str(data.get("reason") or ""),
            "confidence": data.get("confidence"),
        }
        self.heal_log.add(
            HealEvent(
                tool=tool_name, attempt=attempt, ok=True,
                reason=meta["reason"], ref=ref, element=element,
                confidence=meta["confidence"],
            )
        )
        return new_args, meta

    def reset(self) -> None:
        self.heal_log.reset()
