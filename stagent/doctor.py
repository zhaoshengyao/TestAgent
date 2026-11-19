"""环境自检：在真正跑用例前，把"为什么跑不起来"一次性说清楚。"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from .config import Config, load_config

OK = "✅"
BAD = "❌"
WARN = "⚠️"


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=(os.name == "nt"),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def _check_node() -> list[str]:
    lines: list[str] = []
    node = shutil.which("node")
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not node:
        lines.append(f"{BAD} Node.js：未找到。请安装 Node.js 16+（https://nodejs.org/）")
    else:
        code, out = _run(["node", "--version"])
        lines.append(f"{OK if code == 0 else BAD} Node.js：{out or '版本未知'}（{node}）")
    if not npx:
        lines.append(f"{BAD} npx：未找到，Playwright MCP 无法启动")
    else:
        lines.append(f"{OK} npx：{npx}")
    return lines


def _check_ollama(cfg: Config) -> list[str]:
    base = cfg.llm.base_url.rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    try:
        with urllib.request.urlopen(f"{root}/api/tags", timeout=5) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
    except Exception as exc:
        return [f"{WARN} Ollama：{root} 不可达（{type(exc).__name__}）。若用云端或内网模型可忽略。"]
    models = [m.get("name", "") for m in payload.get("models", []) or []]
    lines = [f"{OK} Ollama：{root} 在线，已安装 {len(models)} 个模型"]
    wanted = cfg.llm.model
    hit = any(m == wanted or m.startswith(wanted.split(":")[0]) for m in models)
    if hit:
        lines.append(f"{OK} 目标模型：{wanted} 已就绪")
    else:
        lines.append(
            f"{WARN} 目标模型：{wanted} 未在本地找到。执行 `ollama pull {wanted}`，"
            f"或改用已安装的：{', '.join(models[:6]) or '无'}"
        )
    return lines


def _check_browsers() -> list[str]:
    path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if path:
        candidate = Path(path)
    elif os.name == "nt":
        candidate = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    else:
        candidate = Path.home() / ".cache" / "ms-playwright"
    if not candidate.exists():
        return [
            f"{BAD} Playwright 浏览器：未找到 {candidate}。执行 `npx playwright install chromium`"
        ]
    items = [p.name for p in candidate.iterdir() if p.is_dir()]
    chromium = [i for i in items if "chromium" in i]
    if chromium:
        return [f"{OK} Playwright 浏览器：{candidate}（{', '.join(chromium[:3])}）"]
    return [f"{WARN} Playwright 浏览器目录存在但没有 chromium：{candidate}"]


def _check_remote_llm(cfg: Config) -> list[str]:
    """通用远端 LLM 检查：deepseek / 任意内网 custom 端点。"""
    api_key_ok = bool(cfg.llm.api_key) and cfg.llm.api_key != "ollama"
    lines = [
        f"{WARN} 远端模型：请确认 base_url / api_key / model 三项正确。"
        f"（当前 api_key={'已填' if api_key_ok else '未填或用了占位符'}）"
    ]
    if not api_key_ok:
        lines.append(f"{BAD} api_key 未配置或为占位字符串，远端请求会被 401 拒绝。")
    return lines


def run_doctor(config_path: str | None = None) -> int:
    cfg = load_config(config_path)
    print("=" * 60)
    print("stagent 环境自检")
    print("=" * 60)
    print(f"{OK} Python：{sys.version.split()[0]}（{platform.system()} {platform.machine()}）")
    print(f"{OK} 工作目录：{cfg.base_dir}")
    print(f"{'✅' if cfg.config_path else '⚠️'} 配置文件：{cfg.config_path or '未找到 config.yaml，使用内置默认值'}")
    print(f"   LLM：{cfg.llm.provider} / {cfg.llm.model} @ {cfg.llm.base_url}")
    print(f"   浏览器：{'无头' if cfg.browser.headless else '有头'}，截图目录 {cfg.screens_dir}")
    print("-" * 60)

    # 帮助排查：如果环境变量覆盖了 yaml，打印出来
    covered = []
    for env_key in ("STAGENT_PROVIDER", "STAGENT_MODEL", "STAGENT_BASE_URL", "STAGENT_API_KEY"):
        if env_key in os.environ:
            covered.append(env_key)
    if covered:
        print(f"{WARN} 以下环境变量会覆盖 config.yaml：{', '.join(covered)}")
        print(f"       用 `Remove-Item Env:{covered[0]}` 清除，或直接改环境变量值。")
        print("-" * 60)

    problems = 0
    for line in _check_node():
        print(line)
        if line.startswith(BAD):
            problems += 1

    if cfg.llm.provider == "ollama":
        for line in _check_ollama(cfg):
            print(line)
            if line.startswith(WARN):
                problems += 0  # 只是警告，不阻塞
    else:
        for line in _check_remote_llm(cfg):
            print(line)
            if line.startswith(BAD):
                problems += 1

    for line in _check_browsers():
        print(line)
        if line.startswith(BAD):
            problems += 0  # 首次运行 MCP 会自动安装，给提示即可

    print("-" * 60)
    if problems:
        print(f"{BAD} 有 {problems} 个阻塞项，先解决上面的 ❌ 再跑用例。")
        return 1
    print(f"{OK} 环境看起来可以跑。下一步：python -m stagent run suites/smoke_login.yaml")
    return 0
