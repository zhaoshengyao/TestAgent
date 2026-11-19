"""Playwright MCP 客户端桥接。

它做两件事（对应文章里 MCP 的两个价值）：
1. 工具暴露：把 @playwright/mcp 暴露的 browser_* 工具转成 OpenAI function calling 的 schema；
2. 上下文提供：把工具返回的页面快照、截图、console 日志收拢成模型可读的文本。
"""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import BrowserConfig


@dataclass
class ToolCallResult:
    text: str = ""
    is_error: bool = False
    images: list[Path] = field(default_factory=list)
    raw: Any = None


class MCPBridge:
    def __init__(self, cfg: BrowserConfig, base_dir: Path, screens_dir: Path | None = None):
        self.cfg = cfg
        self.base_dir = base_dir
        self.screens_dir = (screens_dir or (base_dir / cfg.output_dir)).resolve()
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: list[Any] = []
        self._image_seq = 0
        self._started_at = 0.0

    # ---------------------------------------------------------------- 启动/关闭
    def _resolve_command(self) -> str:
        if self.cfg.command:
            return self.cfg.command
        for candidate in ("npx.cmd", "npx", "npx.exe"):
            found = shutil.which(candidate)
            if found:
                return found
        raise RuntimeError(
            "找不到 npx。请先安装 Node.js 16+ 并确认 `npx -v` 可用，"
            "或在 config.yaml 的 browser.command 里写死 npx 的绝对路径。"
        )

    def _build_args(self) -> list[str]:
        args = list(self.cfg.args) + list(self.cfg.extra_args)
        self.screens_dir.mkdir(parents=True, exist_ok=True)
        if not any(str(a).startswith("--output-dir") for a in args):
            args.append(f"--output-dir={self.screens_dir}")
        if self.cfg.headless and "--headless" not in args:
            args.append("--headless")
        return args

    async def start(self) -> None:
        command = self._resolve_command()
        args = self._build_args()
        env = dict(os.environ)
        env.setdefault("npm_config_yes", "true")  # 避免 npx 首次安装时卡在确认提示

        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=command, args=args, env=env)
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await asyncio.wait_for(self.session.initialize(), timeout=self.cfg.startup_timeout_s)
        listed = await self.session.list_tools()
        self.tools = list(getattr(listed, "tools", []) or [])
        self._started_at = time.time()
        if not self.tools:
            raise RuntimeError("Playwright MCP 已启动但没有暴露任何工具，请检查 @playwright/mcp 版本。")

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
        self.session = None

    @property
    def tool_names(self) -> list[str]:
        return [getattr(t, "name", "") for t in self.tools]

    # ---------------------------------------------------------------- 工具 schema
    def openai_tools(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tool in self.tools:
            name = getattr(tool, "name", "")
            if not name:
                continue
            schema = getattr(tool, "inputSchema", None) or {
                "type": "object",
                "properties": {},
            }
            schema = _clean_schema(dict(schema))
            description = (getattr(tool, "description", "") or "")[:1200]
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": schema,
                    },
                }
            )
        return out

    # ---------------------------------------------------------------- 调用
    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolCallResult:
        if self.session is None:
            return ToolCallResult(text="MCP 会话尚未启动", is_error=True)
        arguments = arguments or {}
        try:
            result = await asyncio.wait_for(
                self.session.call_tool(name, arguments),
                timeout=self.cfg.tool_timeout_s,
            )
        except asyncio.TimeoutError:
            return ToolCallResult(
                text=f"工具 {name} 执行超时（>{self.cfg.tool_timeout_s:.0f}s）", is_error=True
            )
        except Exception as exc:
            return ToolCallResult(text=f"工具 {name} 调用异常：{exc}", is_error=True)

        return self._flatten(result, tool_name=name, arguments=arguments)

    def _flatten(self, result: Any, tool_name: str, arguments: dict[str, Any]) -> ToolCallResult:
        texts: list[str] = []
        images: list[Path] = []
        for item in getattr(result, "content", []) or []:
            item_type = getattr(item, "type", None)
            if item_type == "text":
                texts.append(getattr(item, "text", "") or "")
            elif item_type == "image":
                path = self._save_image(getattr(item, "data", ""), getattr(item, "mimeType", "image/png"))
                if path:
                    images.append(path)
                    texts.append(f"[已保存截图：{path.name}]")
            else:
                texts.append(str(item))

        # 有些动作只返回截图文件名，没有 image content
        if not images and tool_name == "browser_take_screenshot":
            filename = arguments.get("filename")
            if isinstance(filename, str) and filename:
                candidate = Path(filename)
                if not candidate.is_absolute():
                    candidate = self.screens_dir / candidate
                if candidate.exists():
                    images.append(candidate.resolve())

        return ToolCallResult(
            text="\n".join(t for t in texts if t).strip(),
            is_error=bool(getattr(result, "isError", False)),
            images=images,
            raw=result,
        )

    def _save_image(self, data: str, mime: str) -> Path | None:
        if not data:
            return None
        suffix = ".png"
        if "jpeg" in (mime or "") or "jpg" in (mime or ""):
            suffix = ".jpg"
        self._image_seq += 1
        path = self.screens_dir / f"mcp_image_{self._image_seq:03d}_{int(time.time())}{suffix}"
        try:
            path.write_bytes(base64.b64decode(data))
        except Exception:
            return None
        return path


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 对多余字段比较敏感，去掉 $schema / additionalProperties 之外的噪声。"""
    schema.pop("$schema", None)
    schema.setdefault("type", "object")
    props = schema.get("properties")
    if isinstance(props, dict):
        for value in props.values():
            if isinstance(value, dict):
                value.pop("$schema", None)
    return schema
