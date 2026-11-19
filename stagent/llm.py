"""LLM 适配层：统一走 OpenAI 兼容协议。

- 本地 Ollama：http://localhost:11434/v1（qwen3 / qwen2.5 都支持 function calling）
- DeepSeek / 其他：改 base_url 与 api_key 即可

内置能力：
1. 自动重试 429 / 5xx / 超时，带指数退避（1s → 2s → 4s → 8s → 16s ... 封顶 60s）。
2. 429 时尊重响应头里的 Retry-After，有就按它等。
3. JSON 输出里抠第一个 { ... } 容忍 ```json 围栏。
4. 用例间加延迟（inter_call_jitter）避免连续请求击穿网关。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any

from openai import AsyncOpenAI
from openai import APIError, APIConnectionError, RateLimitError

from .config import LLMConfig


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、超时、限流等）。"""


class RateLimitedError(LLMError):
    """429 Too Many Requests —— 可以重试。"""


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象，容忍 ```json 围栏与前后废话。"""
    if not text:
        raise ValueError("模型返回为空，无法解析 JSON")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"模型输出里没有 JSON 对象：{text[:200]}")
    return json.loads(cleaned[start : end + 1])


class LLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        kwargs: dict[str, Any] = {
            "base_url": cfg.base_url,
            "api_key": cfg.api_key or "not-needed",
            "timeout": cfg.timeout_s,
            "max_retries": 1,  # SDK 内置只重试一次，外层我们自己来
        }
        self.client = AsyncOpenAI(**kwargs)
        # 节流参数（可通过 config.yaml → llm 段调整）
        self._min_interval = cfg.min_interval_s
        self._max_attempts = cfg.max_attempts
        self._base_delay = cfg.base_delay_s
        self._max_delay = cfg.max_delay_s
        self._last_call = 0.0
        # 最大重试次数（外层）
        self._max_attempts = 12

    # ---------------------------------------------------------------- 节流
    async def _throttle(self) -> None:
        """保证连续两次调用之间至少 _min_interval 秒。"""
        elapsed = time.time() - self._last_call
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed + random.uniform(0, 0.5))
        self._last_call = time.time()

    # ---------------------------------------------------------------- 重试循环
    async def _call_with_retry(
        self,
        payload: dict[str, Any],
    ) -> Any:
        """包装 SDK 调用，对 429 / 5xx / 连接错误做激进的指数退避重试。"""
        last_exc: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            await self._throttle()
            try:
                return await self.client.chat.completions.create(**payload)
            except RateLimitError as exc:
                last_exc = exc
                # 尊重 Retry-After 头
                retry_after: float | None = None
                resp = getattr(exc, "response", None)
                headers = getattr(resp, "headers", None) or {}
                if headers:
                    ra = headers.get("Retry-After") or headers.get("retry-after")
                    if ra:
                        try:
                            retry_after = float(ra)
                        except (TypeError, ValueError):
                            pass
                delay = (
                    retry_after
                    if retry_after
                    else self._base_delay * (2 ** min(attempt - 1, 6))
                )
                delay = min(delay, self._max_delay)
                delay += random.uniform(0, 1.0)  # 加一点抖动避免并发重试
                if attempt == self._max_attempts:
                    break
                await asyncio.sleep(delay)
            except APIConnectionError as exc:
                last_exc = exc
                if attempt == self._max_attempts:
                    break
                delay = min(self._base_delay * (2 ** min(attempt - 1, 5)), 30.0)
                await asyncio.sleep(delay + random.uniform(0, 0.5))
            except APIError as exc:
                status = getattr(exc, "status_code", None)
                if isinstance(status, int) and 500 <= status < 600:
                    last_exc = exc
                    if attempt == self._max_attempts:
                        break
                    delay = min(self._base_delay * (2 ** min(attempt - 1, 4)), 20.0)
                    await asyncio.sleep(delay + random.uniform(0, 0.5))
                else:
                    raise LLMError(
                        f"模型调用失败（{self.cfg.model} @ {self.cfg.base_url}）：{exc}"
                    ) from exc
            except Exception as exc:
                raise LLMError(
                    f"模型调用失败（{self.cfg.model} @ {self.cfg.base_url}）：{exc}"
                ) from exc

        raise LLMError(
            f"模型调用失败（{self.cfg.model} @ {self.cfg.base_url}）：{last_exc}"
        ) from last_exc

    # ---------------------------------------------------------------- 对外接口
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """一次对话，返回归一化的 assistant 消息 dict。"""
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            # 不传 tool_choice：OpenAI 默认就是 auto，而部分本地推理端不认识该字段

        resp = await self._call_with_retry(payload)

        if not resp.choices:
            raise LLMError("模型没有返回任何 choice")

        message = resp.choices[0].message
        out: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if message.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in message.tool_calls
            ]
        return out

    async def chat_text(
        self,
        system: str,
        user: str,
        temperature: float | None = 0.0,
    ) -> str:
        msg = await self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            tools=None,
            temperature=temperature,
        )
        return (msg.get("content") or "").strip()

    async def chat_json(self, system: str, user: str) -> dict[str, Any]:
        text = await self.chat_text(system, user, temperature=0.0)
        return extract_json(text)
