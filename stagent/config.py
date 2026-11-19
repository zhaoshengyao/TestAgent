"""配置加载：config.yaml → .env / 环境变量（后者只在前者没写时才生效）。

优先级（从高到低）：
  1. config.yaml 的 llm / browser / agent 段（项目级权威配置）
  2. 进程环境变量（os.environ）
  3. .env 文件里的值（load_dotenv 只会 setdefault，不覆盖已有键）
  4. 内置默认值

换句人话：**yaml 里写了什么就是什么**，.env 和环境变量只在 yaml 里没写某字段时才提供默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 各 provider 的默认连接参数
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "model": "qwen3:latest",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
    },
}


@dataclass
class LLMConfig:
    provider: str = "ollama"
    model: str = "qwen3:latest"
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout_s: float = 240.0
    # 节流：连续两次 LLM 调用之间至少隔 min_interval_s 秒（防内网网关限流）
    min_interval_s: float = 2.0
    # 指数退避：429 时最多重试 max_attempts 次，每次 sleep base_delay_s * 2^(attempt-1)
    max_attempts: int = 12
    base_delay_s: float = 2.0
    # 单次退避封顶（防止 sleep 太久）
    max_delay_s: float = 60.0


@dataclass
class BrowserConfig:
    command: str = ""
    args: list[str] = field(default_factory=lambda: ["@playwright/mcp@latest"])
    headless: bool = True
    output_dir: str = "artifacts/screens"
    startup_timeout_s: float = 120.0
    tool_timeout_s: float = 60.0
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    max_steps: int = 40
    heal_attempts: int = 2
    auto_observe: bool = False
    tool_result_char_limit: int = 6000
    screenshot_each_step: bool = True


@dataclass
class Config:
    llm: LLMConfig
    browser: BrowserConfig
    agent: AgentConfig
    base_dir: Path
    config_path: Path | None = None

    @property
    def screens_dir(self) -> Path:
        return (self.base_dir / self.browser.output_dir).resolve()


def load_dotenv(path: Path) -> None:
    """极简 .env 解析：只认 KEY=VALUE 与 # 注释，已存在的环境变量优先。"""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _as_list(value: Any, default: list[str]) -> list[str]:
    """容忍 YAML 里把列表写成单个字符串。"""
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _first_nonempty(*values: Any) -> Any:
    """返回第一个非空（非 None、非空字符串、非空列表）的值。"""
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        if isinstance(v, (list, tuple)) and len(v) == 0:
            continue
        return v
    return None


def load_config(config_path: str | os.PathLike[str] | None = None) -> Config:
    """加载配置。config_path 可以是文件路径，也可以是目录。"""
    raw_path = config_path or os.environ.get("STAGENT_CONFIG") or "config.yaml"
    path = Path(raw_path).expanduser()
    if path.is_dir():
        path = path / "config.yaml"
    path = path.resolve()
    base_dir = path.parent

    # 先读 yaml（权威配置）
    raw: dict[str, Any] = {}
    if path.exists() and path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except yaml.YAMLError as exc:
            raise SystemExit(f"配置文件解析失败：{path}\n{exc}") from exc

    # 再用 .env 填充进程环境（setdefault，不覆盖已有键）
    load_dotenv(base_dir / ".env")

    def pick(section: str, key: str) -> Any:
        """只从 yaml raw 里拿。"""
        sec = raw.get(section)
        if isinstance(sec, dict) and key in sec:
            return sec[key]
        if key in raw:
            return raw[key]
        return None

    provider_raw = _first_nonempty(
        pick("llm", "provider"),
        os.environ.get("STAGENT_PROVIDER"),
        "ollama",
    )
    provider = str(provider_raw).strip()
    preset = PROVIDER_PRESETS.get(provider, {})

    model = str(_first_nonempty(
        pick("llm", "model"),
        os.environ.get("STAGENT_MODEL"),
        preset.get("model", "qwen3:latest"),
    ))

    base_url = str(_first_nonempty(
        pick("llm", "base_url"),
        os.environ.get("STAGENT_BASE_URL"),
        preset.get("base_url", "http://localhost:11434/v1"),
    ))

    api_key = str(_first_nonempty(
        pick("llm", "api_key"),
        os.environ.get("STAGENT_API_KEY"),
        os.environ.get("OPENAI_API_KEY"),
        preset.get("api_key", "ollama"),
    ))

    temperature = float(_first_nonempty(pick("llm", "temperature"), 0.2))
    max_tokens = int(_first_nonempty(pick("llm", "max_tokens"), 2048))
    timeout_s = float(_first_nonempty(pick("llm", "timeout_s"), 240))
    min_interval_s = float(_first_nonempty(pick("llm", "min_interval_s"), 2.0))
    max_attempts = int(_first_nonempty(pick("llm", "max_attempts"), 12))
    base_delay_s = float(_first_nonempty(pick("llm", "base_delay_s"), 2.0))
    max_delay_s = float(_first_nonempty(pick("llm", "max_delay_s"), 60.0))

    llm = LLMConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        min_interval_s=min_interval_s,
        max_attempts=max_attempts,
        base_delay_s=base_delay_s,
        max_delay_s=max_delay_s,
    )

    browser = BrowserConfig(
        command=str(_first_nonempty(pick("browser", "command"), "") or ""),
        args=_as_list(_first_nonempty(pick("browser", "args"), ["@playwright/mcp@latest"]), ["@playwright/mcp@latest"]),
        headless=_to_bool(_first_nonempty(pick("browser", "headless"), os.environ.get("STAGENT_HEADLESS"), True), True),
        output_dir=str(_first_nonempty(pick("browser", "output_dir"), "artifacts/screens")),
        startup_timeout_s=float(_first_nonempty(pick("browser", "startup_timeout_s"), 120)),
        tool_timeout_s=float(_first_nonempty(pick("browser", "tool_timeout_s"), 60)),
        extra_args=[str(a) for a in _as_list(_first_nonempty(pick("browser", "extra_args"), []), [])],
    )

    agent = AgentConfig(
        max_steps=int(_first_nonempty(pick("agent", "max_steps"), 40)),
        heal_attempts=int(_first_nonempty(pick("agent", "heal_attempts"), 2)),
        auto_observe=_to_bool(_first_nonempty(pick("agent", "auto_observe"), False), False),
        tool_result_char_limit=int(_first_nonempty(pick("agent", "tool_result_char_limit"), 6000)),
        screenshot_each_step=_to_bool(_first_nonempty(pick("agent", "screenshot_each_step"), True), True),
    )

    return Config(
        llm=llm,
        browser=browser,
        agent=agent,
        base_dir=base_dir,
        config_path=path if path.exists() else None,
    )
