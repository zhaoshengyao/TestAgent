"""Playwright MCP 返回的页面快照（accessibility tree）解析。

@playwright/mcp 的 browser_snapshot 输出形如：

    - generic [ref=e1]:
      - heading "Example Domain" [level=1] [ref=e3]
      - textbox "Username" [ref=e5]:
      - button "Login" [ref=e7]

这里把每一行拆成 (role, name, ref)，用于：
1. 自愈定位时候选元素列表；
2. 给模型一条"可读的元素清单"，避免它去猜 CSS 选择器。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_LINE_RE = re.compile(r"^(?P<indent>\s*)-\s+(?P<rest>.+)$")
_REF_RE = re.compile(r"\[ref=(?P<ref>[^\]\s]+)\]")
_QUOTED_RE = re.compile(r'"(?P<name>[^"]*)"')
_ROLE_RE = re.compile(r"^[A-Za-z][\w:-]*")

# 可交互角色，自愈时优先在这类元素里找
INTERACTIVE_ROLES = {
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "checkbox",
    "radio",
    "switch",
    "slider",
    "spinbutton",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "option",
    "tab",
    "treeitem",
    "listbox",
    "listitem",
    "heading",
    "dialog",
    "alertdialog",
    "img",
}


@dataclass(frozen=True)
class Element:
    role: str
    name: str
    ref: str
    line: str

    def render(self) -> str:
        label = f' "{self.name}"' if self.name else ""
        return f"- {self.role}{label} [ref={self.ref}]"


def parse_snapshot(text: str) -> list[Element]:
    """从快照文本里抽出所有带 ref 的元素，保持原始顺序。

    跳过 generic / text / paragraph 等纯容器或纯文本角色——它们不是
    可交互节点，也不参与自愈定位的候选集。
    """
    SKIP_ROLES = {"generic", "text", "paragraph", "InlineTextBox"}
    elements: list[Element] = []
    if not text:
        return elements
    for raw_line in text.splitlines():
        match = _LINE_RE.match(raw_line)
        if not match:
            continue
        rest = match.group("rest").strip()
        ref_match = _REF_RE.search(rest)
        if not ref_match:
            continue
        ref = ref_match.group("ref").strip()
        role_match = _ROLE_RE.match(rest)
        role = role_match.group(0) if role_match else "unknown"
        if role in SKIP_ROLES:
            continue
        quoted = _QUOTED_RE.search(rest)
        name = quoted.group("name").strip() if quoted else ""
        elements.append(Element(role=role, name=name, ref=ref, line=raw_line.rstrip()))
    return elements


def interactive_elements(elements: list[Element]) -> list[Element]:
    """只保留可交互元素；空的 generic/text 节点对定位没帮助。"""
    picked = [e for e in elements if e.role in INTERACTIVE_ROLES]
    return picked or elements


def format_elements(elements: list[Element], limit: int = 120) -> str:
    lines = [e.render() for e in elements[:limit]]
    if len(elements) > limit:
        lines.append(f"...（共 {len(elements)} 个元素，已省略 {len(elements) - limit} 个）")
    return "\n".join(lines)


def compact_snapshot(text: str, limit: int = 2500) -> str:
    """把整棵快照压缩成"可交互元素清单"，用于注入上下文。"""
    elements = interactive_elements(parse_snapshot(text))
    if not elements:
        return (text or "")[:limit]
    rendered = format_elements(elements, limit=80)
    return rendered[:limit]
