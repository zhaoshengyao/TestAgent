"""交互式向导：init → 探测页面 → 生成 suite.yaml + config.yaml。

不依赖 LLM，只用 Playwright MCP 的 snapshot 发现可交互元素。

用法：
  python -m stagent init --url https://www.saucedemo.com --dir ./my-test
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from .config import Config, load_config
from .mcp_bridge import MCPBridge
from .suite import Case


async def _discover(cfg: Config, url: str) -> dict:
    """打开 URL，跑 snapshot，解析可交互元素。"""
    bridge = MCPBridge(cfg.browser, cfg.base_dir, cfg.screens_dir)
    try:
        await bridge.start()
    except Exception as exc:
        print(f"✗ MCP 启动失败：{exc}")
        print("  请先跑：python -m stagent doctor")
        return {}

    try:
        nav = await bridge.call("browser_navigate", {"url": url})
        if nav.is_error:
            print(f"✗ 无法打开 {url}：{nav.text[:200]}")
            return {}

        snap = await bridge.call("browser_snapshot", {})
        if snap.is_error:
            print(f"✗ snapshot 失败：{snap.text[:200]}")
            return {}

        # 解析 snapshot 里的交互元素（行格式：- type "label" [ref=xxx]）
        text = snap.text or ""
        interactive = []
        for line in text.splitlines():
            m = re.match(r'-\s+(\S+)\s+"([^"]*)"\s+\[ref=(\S+)\]', line.strip())
            if m:
                etype, label, ref = m.group(1), m.group(2), m.group(3)
                interactive.append({"type": etype, "label": label, "ref": ref})

        return {"url": url, "title": "", "interactive": interactive, "snapshot": text}
    finally:
        await bridge.close()


def _classify(elements: list[dict]) -> dict:
    """把交互元素分类：登录框 / 表单 / 导航按钮 / 其他。"""
    result = {"username": [], "password": [], "login_btn": [], "forms": [], "nav": [], "other": []}
    for e in elements:
        label = e["label"].lower()
        etype = e["type"].lower()
        if etype == "textbox" and ("user" in label or "name" in label or "账号" in label):
            result["username"].append(e)
        elif etype == "textbox" and ("pass" in label or "密码" in label):
            result["password"].append(e)
        elif etype in ("button", "submit") and ("login" in label or "sign" in label or "登录" in label):
            result["login_btn"].append(e)
        elif etype in ("button", "submit") and etype in ("textbox",):
            result["forms"].append(e)
        elif etype in ("link", "button") and any(k in label for k in ("dashboard", "home", "homepage", "仪表盘", "首页")):
            result["nav"].append(e)
        else:
            result["other"].append(e)
    return result


def _print_discover(disc: dict):
    """漂亮打印探测结果。"""
    if not disc:
        return
    print(f"\n🌐 探测：{disc['url']}")
    print(f"📋 共 {len(disc['interactive'])} 个可交互元素\n")
    cls = _classify(disc["interactive"])

    groups = [
        ("登录相关", cls["username"] + cls["password"] + cls["login_btn"]),
        ("表单字段", cls["forms"]),
        ("导航/菜单", cls["nav"]),
        ("其他", cls["other"][:15]),
    ]
    for gname, items in groups:
        if not items:
            continue
        print(f"  📁 {gname}（{len(items)}）")
        for e in items:
            print(f"    {e['type']:12s}  \"{e['label']}\"  [ref={e['ref']}]")
        print()


def _build_login_case(disc: dict, username: str, password: str) -> Case | None:
    """根据探测结果 + 用户输入，自动构造登录 case。"""
    cls = _classify(disc["interactive"])
    if not cls["username"] or not cls["password"] or not cls["login_btn"]:
        return None

    return Case(
        name="登录冒烟",
        url=disc["url"],
        instruction=(
            f"用 {username} / {password} 登录。\n"
            f"1. 填充用户名和密码\n"
            f"2. 点击登录按钮\n"
            f"3. 确认登录成功（URL 跳转或页面出现退出按钮/用户信息）\n"
            f"4. 截图留证"
        ),
        assertions=[
            "登录后 URL 变化",
            "页面出现退出按钮或用户信息",
        ],
        timeout_s=1800,
    )


def _build_suite_yaml(url: str, username: str, password: str, app_url_placeholder: str = "{{app_url}}") -> str:
    """生成最小可用的 suite.yaml 骨架。"""
    # 如果用户 URL 包含敏感信息（localhost/内网 IP），app_url 直接用
    actual_url = url if url.startswith("http") else app_url_placeholder

    return f"""# TestAgent 用例集（stagent init 自动生成）
name: {url.rstrip('/').replace('https://', '').replace('http://', '')} 回归测试
app_url: "{actual_url}"
standard_username: "{username}"
standard_password: "{password}"

cases:
  # ── 登录冒烟（自动生成）──
  - name: "登录冒烟"
    url: {actual_url}
    instruction: |
      用 {username} / {password} 登录。
      步骤：
      1. 打开 {actual_url}
      2. 填充用户名和密码
      3. 点击登录
      4. 确认登录成功（URL 跳转 / 出现退出按钮 / 出现用户信息）
      5. 截图
    assertions:
      - "登录后 URL 变化（跳转到非登录页）"
      - "页面出现退出按钮或用户信息"
    timeout_s: 1800

  # ── TODO：你的业务流程（手动补）──
  # - name: "创建流程"
  #   url: {actual_url}
  #   instruction: |
  #     1. 登录
  #     2. 点击"创建"按钮
  #     3. 填写表单
  #     4. 提交
  #     5. 确认创建成功
  #   assertions:
  #     - "页面出现创建成功提示"
  #     - "列表页出现新记录"
  #   timeout_s: 1800
  #
  # - name: "编辑流程"
  #   url: {actual_url}
  #   ...
"""


def _build_config_yaml(url: str, username: str, password: str) -> str:
    """生成 config.yaml（留空 LLM 字段让用户填）。"""
    return f"""# TestAgent 配置（stagent init 自动生成）
# LLM 配置：provider / base_url / model / api_key 任选一个来源
#   1. 填在 yaml 里（明文，仅本地调试）
#   2. 通过环境变量 STAGENT_BASE_URL / STAGENT_MODEL / STAGENT_API_KEY 注入（CI 推荐）

llm:
  provider: custom          # ollama / custom
  base_url: ""              # 留空用环境变量
  model: ""                 # 留空用环境变量
  api_key: ""               # 留空用环境变量
  temperature: 0.2
  max_tokens: 4096
  timeout_s: 240

agent:
  max_steps: 40
  heal_attempts: 2
  auto_observe: false

browser:
  headless: false            # init 阶段建议有头，看得到浏览器
  startup_timeout_s: 120
  tool_timeout_s: 60
"""


async def cmd_init(args: argparse.Namespace) -> int:
    """init 主流程。"""
    url = args.url
    # 防止用户粘贴两次 https://
    url = re.sub(r'^(https?://)+', 'https://', url)
    out_dir = Path(args.dir).resolve()
    yes = args.yes

    if not url.startswith(("http://", "https://")):
        print("✗ --url 必须是 http:// 或 https:// 开头的完整地址")
        return 1

    print(f"\n🚀 stagent init")
    print(f"🌐 目标：{url}")
    print(f"📁 输出目录：{out_dir}")

    # Step 1: 探测页面（load_config 而不是 Config()）
    print("\n── Step 1: 探测页面可交互元素 ──")
    cfg = load_config()
    disc = await _discover(cfg, url)
    if not disc:
        print("\n❌ 探测失败。可能原因：")
        print("   - URL 打不开")
        print("   - 页面需要登录才能看到内容")
        print("   - 浏览器/LLM 未配置")
        return 1

    _print_discover(disc)

    # Step 2: 收集账号密码
    cls = _classify(disc["interactive"])
    if cls["username"] and cls["password"] and cls["login_btn"]:
        print("── Step 2: 填写测试账号 ──")
        if yes:
            username = args.username or "admin"
            password = args.password or "admin"
            print(f"  username: {username}")
            print(f"  password: {password}")
        else:
            username = input("  测试账号 [admin]: ").strip() or "admin"
            password = input("  测试密码 [admin]: ").strip() or "admin"
    else:
        username = args.username or ""
        password = args.password or ""
        print("\n⚠️  未检测到登录表单，跳过账号配置。")

    # Step 3: 生成文件
    print(f"\n── Step 3: 生成文件到 {out_dir} ──")
    out_dir.mkdir(parents=True, exist_ok=True)

    suite_path = out_dir / "suite.yaml"
    config_path = out_dir / "config.yaml"

    suite_path.write_text(_build_suite_yaml(url, username, password), encoding="utf-8")
    config_path.write_text(_build_config_yaml(url, username, password), encoding="utf-8")

    print(f"  ✅ {suite_path}")
    print(f"  ✅ {config_path}")

    # Step 4: 打印下一步
    print("\n── Step 4: 下一步 ──")
    print(f"  1. 填 config.yaml 里的 LLM 配置")
    print(f"     (或用环境变量 STAGENT_BASE_URL / STAGENT_MODEL / STAGENT_API_KEY)")
    print(f"  2. 跑登录冒烟：")
    print(f"     cd {out_dir}")
    print(f"     python -m stagent run suite.yaml --config config.yaml")
    print(f"\n  3. 报告在：{out_dir}/artifacts/runs/<时间戳>/report.html")
    print(f"\n🎉 init 完成！\n")
    return 0


def add_init_parser(sub: argparse._SubParsersAction):
    """注册 init 子命令。"""
    p = sub.add_parser("init", help="交互式向导：探测页面 → 生成 suite.yaml + config.yaml")
    p.add_argument("--url", required=True, help="目标地址，例如 https://www.google.com")
    p.add_argument("--dir", default="./", help="输出目录（默认当前目录）")
    p.add_argument("--username", default="", help="测试账号（可自动探测）")
    p.add_argument("--password", default="", help="测试密码")
    p.add_argument("--yes", "-y", action="store_true", help="跳过交互，用默认值")
    return p
