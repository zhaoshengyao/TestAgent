"""交互式向导：init → 探测页面 → 自动登录 → 生成 suite.yaml + config.yaml。

不依赖 LLM，只用 Playwright MCP 的 snapshot 发现可交互元素。

用法：
  python -m stagent init --url https://www.saucedemo.com --dir ./my-test
  python -m stagent init --url https://192.168.2.68:6088/ --dir ./client-test -y
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


def _parse_snapshot(text: str) -> list[dict]:
    """解析 snapshot 文本，提取交互元素列表。

    行格式示例：
      - textbox "Username" [ref=e11]
      - button "Login" [ref=e15]
    """
    interactive = []
    for line in text.splitlines():
        m = re.match(r'-\s+(\S+)\s+"([^"]*)"\s+\[ref=(\S+)\]', line.strip())
        if m:
            etype, label, ref = m.group(1), m.group(2), m.group(3)
            interactive.append({"type": etype, "label": label, "ref": ref})
    return interactive


async def _auto_login_and_discover(
    cfg: Config, url: str, username: str, password: str
) -> dict:
    """打开 URL → 自动填账号密码登录 → 登录后跑 snapshot 探测业务页面。

    返回结构：
      {"pre": {interactive, snapshot}, "post": {interactive, snapshot},
       "after_url": "https://..." , "url": "...原始URL..."}
    """
    bridge = MCPBridge(cfg.browser, cfg.base_dir, cfg.screens_dir)
    result: dict = {"pre": {}, "post": {}, "after_url": "", "url": url}

    try:
        await bridge.start()
    except Exception as exc:
        print(f"✗ MCP 启动失败：{exc}")
        print("  请先跑：python -m stagent doctor")
        return result

    try:
        # 1. 打开页面
        nav = await bridge.call("browser_navigate", {"url": url})
        if nav.is_error:
            print(f"✗ 无法打开 {url}：{nav.text[:200]}")
            return result

        # 2. 登录前探测
        pre_snap = await bridge.call("browser_snapshot", {})
        if not pre_snap.is_error:
            result["pre"] = {
                "interactive": _parse_snapshot(pre_snap.text or ""),
                "snapshot": pre_snap.text or "",
            }

        pre_cls = _classify(result["pre"].get("interactive", []))
        has_login_form = bool(pre_cls["username"] and pre_cls["password"] and pre_cls["login_btn"])

        if has_login_form and username and password:
            print(f"  🔑 探测到登录表单，自动填账号密码登录…")

            user_ref = pre_cls["username"][0]["ref"]
            pass_ref = pre_cls["password"][0]["ref"]
            btn_ref = pre_cls["login_btn"][0]["ref"]

            # 填账号密码
            fill = await bridge.call("browser_fill_form", {
                "fields": [
                    {"target": user_ref, "name": "Username", "type": "textbox", "value": username},
                    {"target": pass_ref, "name": "Password", "type": "textbox", "value": password},
                ]
            })
            if fill.is_error:
                print(f"  ⚠️ 填表失败：{fill.text[:100]}")

            # 点登录
            click = await bridge.call("browser_click", {"ref": btn_ref})
            if click.is_error:
                # 可能需要重新找按钮 ref
                print(f"  ⚠️ 点击登录按钮失败：{click.text[:100]}")

            # 等页面跳转
            await bridge.call("browser_wait_for", {"time": 3})

            # 3. 登录后探测
            post_snap = await bridge.call("browser_snapshot", {})
            if not post_snap.is_error:
                result["post"] = {
                    "interactive": _parse_snapshot(post_snap.text or ""),
                    "snapshot": post_snap.text or "",
                }

            # 拿当前 URL
            eval_result = await bridge.call("browser_evaluate", {"script": "location.href"})
            if not eval_result.is_error and eval_result.text:
                result["after_url"] = eval_result.text.strip().strip('"')

            print(f"  ✅ 登录后 URL: {result['after_url'] or '(同上)'}")
            n = len(result["post"].get("interactive", []))
            print(f"  📋 登录后发现 {n} 个交互元素")
        else:
            if not has_login_form:
                print("  ℹ️ 未探测到登录表单（可能已是登录态或页面本身不需要登录）")
            else:
                print("  ℹ️ 跳过自动登录（未填账号密码）")

    finally:
        await bridge.close()

    return result


def _classify(elements: list[dict]) -> dict:
    """把交互元素分类：登录框 / 表单 / 导航按钮 / 业务按钮 / 其他。"""
    result = {
        "username": [], "password": [], "login_btn": [],
        "nav": [], "biz_btn": [], "form_fields": [], "other": [],
    }
    for e in elements:
        label = e["label"].lower()
        etype = e["type"].lower()

        if etype == "textbox" and ("user" in label or "账号" in label):
            result["username"].append(e)
        elif etype == "textbox" and ("pass" in label or "密码" in label):
            result["password"].append(e)
        elif etype in ("button", "submit") and any(k in label for k in ("login", "sign in", "signin", "登录", "确定")):
            result["login_btn"].append(e)
        elif etype in ("link", "button") and any(k in label for k in ("dashboard", "home", "homepage", "仪表盘", "首页", "退出", "logout")):
            result["nav"].append(e)
        elif etype in ("button", "link") and any(k in label for k in ("create", "新建", "创建", "add", "新增", "save", "保存", "submit", "提交", "删除", "delete", "编辑", "edit")):
            result["biz_btn"].append(e)
        elif etype == "textbox":
            result["form_fields"].append(e)
        else:
            result["other"].append(e)
    return result


def _print_discover(disc: dict, phase: str = "登录前"):
    """漂亮打印探测结果。"""
    interactive = disc.get("interactive", [])
    if not interactive:
        return

    cls = _classify(interactive)

    print(f"\n📁 {phase}页面（{len(interactive)} 个交互元素）")
    groups = [
        ("🔐 登录相关", cls["username"] + cls["password"] + cls["login_btn"]),
        ("🗂️ 导航/菜单", cls["nav"]),
        ("⚡ 业务按钮", cls["biz_btn"]),
        ("📝 表单字段", cls["form_fields"]),
        ("其他", cls["other"][:10]),
    ]
    for gname, items in groups:
        if not items:
            continue
        print(f"  {gname}：")
        for e in items[:8]:
            print(f"    {e['type']:12s} \"{e['label']}\" [ref={e['ref']}]")
        if len(items) > 8:
            print(f"    … 还有 {len(items) - 8} 个")


def _build_suite_yaml(
    base_url: str, username: str, password: str,
    post_cls: dict | None = None,
) -> str:
    """生成 suite.yaml。"""
    safe_name = base_url.rstrip("/").replace("https://", "").replace("http://", "").split("/")[0]

    # 自动从登录后探测生成业务流程建议
    biz_todos = []
    if post_cls:
        if post_cls["biz_btn"]:
            biz_labels = ", ".join(f'"{e["label"]}"' for e in post_cls["biz_btn"][:4])
            biz_todos.append(f"  # 登录后发现业务按钮：{biz_labels}")
            biz_todos.append("  # 可以考虑加 '创建→编辑→删除' 流程")
        if post_cls["nav"]:
            nav_labels = ", ".join(f'"{e["label"]}"' for e in post_cls["nav"][:4])
            biz_todos.append(f"  # 导航菜单：{nav_labels}")
            biz_todos.append("  # 可以考虑加 '各菜单页面跳转不报错' 流程")

    return f"""# TestAgent 用例集（stagent init 自动生成）
name: {safe_name} 回归测试
app_url: "{base_url}"
standard_username: "{username}"
standard_password: "{password}"

cases:
  # ── 登录冒烟（自动生成）──
  - name: "登录冒烟"
    url: {base_url}
    instruction: |
      用 {username} / {password} 登录。
      步骤：
      1. 打开 {base_url}
      2. 填充用户名和密码
      3. 点击登录
      4. 确认登录成功（URL 跳转 / 出现退出按钮 / 出现用户信息）
      5. 截图
    assertions:
      - "登录后 URL 变化（跳转到非登录页）"
      - "页面出现退出按钮或用户信息"
    timeout_s: 1800

  # ── TODO：业务流程（手动补）──
{chr(10).join(biz_todos) if biz_todos else "  # 根据实际探测结果手动补业务流程"}
  # - name: "创建→编辑→删除"
  #   url: {base_url}
  #   instruction: |
  #     1. 登录
  #     2. 点击"创建"按钮
  #     3. 填写表单并提交
  #     4. 点击"编辑"修改字段
  #     5. 提交并确认生效
  #     6. 删除并确认列表更新
  #   assertions:
  #     - "创建后列表出现新记录"
  #     - "编辑后字段值更新"
  #     - "删除后列表不再出现该记录"
  #   timeout_s: 1800
"""


def _build_config_yaml(url: str, username: str, password: str) -> str:
    """生成 config.yaml（留空 LLM 字段让用户填）。"""
    is_internal_https = url.startswith("https://") and (
        "localhost" in url
        or "127.0.0.1" in url
        or re.match(r"https://\d+\.\d+\.\d+\.\d+", url)
    )
    extra = '\n  extra_args:\n    - "--ignore-https-errors"' if is_internal_https else ""

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
  headless: false            # init 阶段建议有头，看得到浏览器{extra}
  startup_timeout_s: 120
  tool_timeout_s: 60
"""


async def cmd_init(args: argparse.Namespace) -> int:
    """init 主流程：探测 → 自动登录 → 生成 suite + config。"""
    url = args.url
    url = re.sub(r'^(https?://)+', 'https://', url)  # 防用户粘贴两次 https://
    out_dir = Path(args.dir).resolve()
    yes = args.yes

    if not url.startswith(("http://", "https://")):
        print("✗ --url 必须是 http:// 或 https:// 开头的完整地址")
        return 1

    print(f"\n🚀 stagent init")
    print(f"🌐 目标：{url}")
    print(f"📁 输出目录：{out_dir}")

    # URL 防自签名证书
    cfg = load_config()
    if url.startswith("https://") and (
        "localhost" in url or "127.0.0.1" in url
        or re.match(r"https://\d+\.\d+\.\d+\.\d+", url)
    ):
        print("  💡 检测到内网 HTTPS，自动忽略 SSL 自签名证书错误")
        if "--ignore-https-errors" not in cfg.browser.extra_args:
            cfg.browser.extra_args.append("--ignore-https-errors")

    # Step 1: 收集账号密码
    print(f"\n── Step 1: 测试账号 ──")
    if yes:
        username = args.username or "admin"
        password = args.password or "admin"
        print(f"  username: {username}")
        print(f"  password: {password}")
    else:
        username = (args.username or input("  测试账号 [admin]: ") or "admin").strip()
        password = (args.password or input("  测试密码 [admin]: ") or "admin").strip()

    # Step 2: 探测 + 自动登录
    print(f"\n── Step 2: 探测页面 + 自动登录 ──")
    disc = await _auto_login_and_discover(cfg, url, username, password)

    _print_discover(disc.get("pre", {}), "登录前")
    if disc.get("post"):
        _print_discover(disc["post"], "登录后")

    # Step 3: 生成文件
    print(f"\n── Step 3: 生成文件到 {out_dir} ──")
    out_dir.mkdir(parents=True, exist_ok=True)

    base_url = disc.get("after_url") or url
    post_cls = _classify(disc.get("post", {}).get("interactive", [])) if disc.get("post") else None

    suite_path = out_dir / "suite.yaml"
    config_path = out_dir / "config.yaml"

    suite_path.write_text(
        _build_suite_yaml(base_url, username, password, post_cls), encoding="utf-8"
    )
    config_path.write_text(_build_config_yaml(url, username, password), encoding="utf-8")

    print(f"  ✅ {suite_path}")
    print(f"  ✅ {config_path}")

    # Step 4: 下一步
    print(f"\n── Step 4: 下一步 ──")
    print(f"  1. 填 config.yaml 里的 LLM 配置")
    print(f"     (或用环境变量 STAGENT_BASE_URL / STAGENT_MODEL / STAGENT_API_KEY)")
    print(f"  2. 跑登录冒烟：")
    print(f"     cd {out_dir}")
    print(f"     python -m stagent run suite.yaml --config config.yaml")
    print(f"\n  3. 报告在：{out_dir}/artifacts/runs/<时间戳>/report.html")
    print(f"\n  💡 登录后业务流程建议已自动写入 suite.yaml 的 TODO 注释")
    print(f"     补完后再跑全量回归")
    print(f"\n🎉 init 完成！\n")
    return 0


def add_init_parser(sub: argparse._SubParsersAction):
    """注册 init 子命令。"""
    p = sub.add_parser("init", help="交互式向导：探测页面 → 自动登录 → 生成 suite.yaml + config.yaml")
    p.add_argument("--url", required=True, help="目标地址，例如 https://www.google.com")
    p.add_argument("--dir", default="./", help="输出目录（默认当前目录）")
    p.add_argument("--username", default="", help="测试账号（默认 admin）")
    p.add_argument("--password", default="", help="测试密码（默认 admin）")
    p.add_argument("--yes", "-y", action="store_true", help="跳过交互，用默认值")
    return p
