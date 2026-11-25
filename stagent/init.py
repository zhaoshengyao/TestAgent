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
    # init 强制独立 session——不受 CI/之前浏览残留 cookies 影响
    cfg.browser.extra_args = [a for a in cfg.browser.extra_args if "--isolated" not in a] + ["--isolated"]

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
            click = await bridge.call("browser_click", {"element": "登录按钮", "target": btn_ref})
            if click.is_error:
                # 可能需要重新找按钮 ref
                print(f"  ⚠️ 点击登录按钮失败：{click.text[:100]}")

            # 等页面跳转
            await bridge.call("browser_wait_for", {"time": 3})

            # 3. 登录后探测（首页）
            post_snap = await bridge.call("browser_snapshot", {})
            if not post_snap.is_error:
                result["post"] = {
                    "interactive": _parse_snapshot(post_snap.text or ""),
                    "snapshot": post_snap.text or "",
                }

            # 4. 遍历顶级菜单点进去再探测——业务按钮（创建/编辑/删除）通常在子页面
            post_cls = _classify(result["post"].get("interactive", []))
            print(f"  🐛 DEBUG: post_cls keys={list(post_cls.keys())} menu_count={len(post_cls.get('menu', []))} total_interactive={len(result['post'].get('interactive', []))}")
            menu_items = post_cls.get("menu", [])[:10]
            if menu_items:
                print(f"  🔍 遍历 {len(menu_items)} 个顶级菜单，探测子页面业务元素…")
                result["sub_pages"] = []
                for menu in menu_items:
                    try:
                        # 点菜单
                        click_r = await bridge.call(
                            "browser_click",
                            {"element": menu["label"], "target": menu["ref"]}
                        )
                        if click_r.is_error:
                            continue
                        await bridge.call("browser_wait_for", {"time": 2})
                        # snapshot
                        sub_snap = await bridge.call("browser_snapshot", {})
                        if sub_snap.is_error:
                            continue
                        sub_interactive = _parse_snapshot(sub_snap.text or "")
                        sub_cls = _classify(sub_interactive)
                        biz_count = len(sub_cls["biz_btn"])
                        if biz_count > 0:
                            print(f"    ✅ {menu['label']}: 发现 {biz_count} 个业务按钮！")
                            result["sub_pages"].append({
                                "menu": menu["label"],
                                "interactive": sub_interactive,
                                "biz_btn": sub_cls["biz_btn"],
                                "menu_items": sub_cls["menu"],
                            })
                        # 点完回首页（点第一个 menu 或 navigate）
                        await bridge.call(
                            "browser_navigate",
                            {"url": result["after_url"] or url}
                        )
                        await bridge.call("browser_wait_for", {"time": 1})
                    except Exception as e:
                        print(f"    ⚠️ 遍历 {menu['label']} 失败：{str(e)[:80]}")
                        continue

                total_sub = len(result.get("sub_pages", []))
                if total_sub == 0:
                    print(f"    ℹ️ 遍历了 {len(menu_items)} 个菜单，但没发现 新建/编辑/删除 类按钮")
                    print(f"       可能这些子页面用的是非标准命名，或需要更深层的二级菜单")
                else:
                    print(f"    ✨ 共发现 {total_sub} 个子页面有业务按钮")
            else:
                print(f"  ℹ️ 登录后没发现顶级菜单（menuitem 类型），跳过子页面遍历")

            # 拿当前 URL（MCP 返回的是完整 tool output，里面值在引号里）
            eval_result = await bridge.call(
                "browser_evaluate",
                {"function": "() => { return location.href; }"}
            )
            if not eval_result.is_error and eval_result.text:
                # browser_evaluate 返回格式是 "### Result\n\"值\"\n### Ran..."
                m = re.search(r'"(https?://[^"\s]+)"', eval_result.text)
                if m:
                    result["after_url"] = m.group(1)
                else:
                    for line in eval_result.text.splitlines():
                        line = line.strip()
                        if line.startswith("http"):
                            result["after_url"] = line.strip('"').strip("'")
                            break

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
    """把交互元素分类——尽量覆盖中文业务系统的常见命名。"""
    result = {
        "username": [], "password": [], "login_btn": [],
        "menu": [], "biz_btn": [], "form_fields": [], "other": [],
    }
    # 业务按钮关键词（覆盖通用 + 财务/企业管理/HR 等垂直系统）
    BIZ_KEYWORDS = (
        # 通用 CRUD
        "create", "新建", "创建", "add", "新增", "添加", "新建", "建立",
        "save", "保存", "submit", "提交", "确认", "确定",
        "delete", "删除", "remove", "移除", "清理", "清空",
        "edit", "编辑", "update", "修改", "变更", "更新",
        "导入", "export", "导出", "import", "download", "upload",
        # 审批流
        "审核", "approve", "audit", "confirm", "审批", "通过", "驳回", "拒绝",
        # 财务/企业管理
        "录入", "登记", "核销", "分配", "调拨", "结转", "摊销", "计提",
        "支付", "付款", "收款", "报销", "申请",
        "查询", "搜索", "筛选", "query", "search", "filter",
        "打印", "preview", "预览", "download",
        "返回", "back", "取消", "cancel", "关闭",
        # 导航类但算业务操作
        "详情", "detail", "查看", "view",
    )
    # 导航关键词（通用）
    NAV_KEYWORDS = (
        "dashboard", "home", "homepage", "仪表盘", "首页", "退出", "logout", "登出",
        "工作台", "驾驶舱", "台账", "中心", "管理", "列表", "详情",
        "menu", "菜单", "导航",
    )

    for e in elements:
        label = e["label"].lower().replace(" ", "")
        etype = e["type"].lower()

        if etype == "textbox" and (
            "user" in label or "账号" in label
            or "用户名" in label or "name" in label
        ):
            result["username"].append(e)
        elif etype == "textbox" and ("pass" in label or "密码" in label):
            result["password"].append(e)
        elif etype in ("button", "submit") and (
            "login" in label or "signin" in label or "登录" in label or "确定" in label
            or "submit" in label or "confirm" in label
        ):
            result["login_btn"].append(e)
        # 导航：menuitem 是顶级菜单；navigation 是面包屑/tab 等局部导航单独归 other
        elif etype == "menuitem" or (
            etype in ("link", "button")
            and any(k in label for k in NAV_KEYWORDS)
        ):
            result["menu"].append(e)
        # 业务按钮：任何类型（button/link/menuitem）带业务关键词
        elif etype in ("button", "link", "menuitem") and any(
            k in label for k in BIZ_KEYWORDS
        ):
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
        ("🗂️ 导航/菜单", cls["menu"]),
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
    sub_pages: list[dict] | None = None,
) -> str:
    """生成 suite.yaml——同时看首页和子页面的探测结果。"""
    safe_name = base_url.rstrip("/").replace("https://", "").replace("http://", "").split("/")[0]

    cases = []
    all_biz = list(post_cls.get("biz_btn", [])) if post_cls else []
    all_menus = list(post_cls.get("menu", [])) if post_cls else []
    sub_page_info = []  # (menu_label, biz_btn_labels)
    if sub_pages:
        for sp in sub_pages:
            biz_labels = [b["label"] for b in sp.get("biz_btn", [])]
            if biz_labels:
                all_biz.extend(sp.get("biz_btn", []))
                sub_page_info.append((sp["menu"], biz_labels))

    # 1. 登录冒烟（必生成）
    cases.append(f"""  - name: "登录冒烟"
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
""")

    # 2. 根据探测到的业务按钮自动生成（综合首页 + 子页面）
    if post_cls or all_biz:
        has_create = any(any(k in e["label"].lower() for k in ("create", "新建", "创建", "add", "新增", "添加")) for e in all_biz)
        has_edit = any(any(k in e["label"].lower() for k in ("edit", "编辑", "修改", "update")) for e in all_biz)
        has_delete = any(any(k in e["label"].lower() for k in ("delete", "删除", "remove", "移除")) for e in all_biz)
        nav_labels = [e["label"] for e in all_menus]

        # 找业务按钮在哪个菜单里
        def biz_in_menu(keyword: str) -> str:
            for menu, btns in sub_page_info:
                if any(keyword in b.lower() or keyword in b for b in btns):
                    return menu
            return ""

        # 2a. 菜单冒烟——遍历所有顶级菜单
        for menu_label in nav_labels[:10]:
            cases.append(f"""  - name: "菜单冒烟：{menu_label}"
    url: {base_url}
    instruction: |
      1. 用 {username} / {password} 登录
      2. 点击左侧/顶部导航的「{menu_label}」
      3. 确认页面加载成功（URL 跳转 / 页面有表格或卡片 / 没有 loading spinner）
    assertions:
      - "点击「{menu_label}」后页面正常加载"
      - "没有报错或空白"
    timeout_s: 1800
""")

        # 2b. 创建流程（带菜单导航步骤）
        if has_create:
            action_label = next((b["label"] for b in all_biz if "创建" in b["label"] or "新建" in b["label"] or "新增" in b["label"] or "create" in b["label"].lower() or "add" in b["label"].lower()), "创建")
            menu_hint = biz_in_menu("创建") or biz_in_menu("新建") or biz_in_menu("新增")
            nav_step = f"      2. 点击左侧/顶部导航的「{menu_hint}」\n" if menu_hint else ""
            cases.append(f"""  - name: "{action_label}流程"
    url: {base_url}
    instruction: |
      1. 用 {username} / {password} 登录
{nav_step}      {'3' if menu_hint else '2'}. 点击「{action_label}」按钮
      {'4' if menu_hint else '3'}. 逐个填写表单里的所有输入框（填合理的测试数据）
      {'5' if menu_hint else '4'}. 点击提交/保存
      {'6' if menu_hint else '5'}. 确认创建成功（URL 跳转 / 成功提示 / 列表出现新记录）
    assertions:
      - "创建后出现成功提示或 URL 变化"
      - "列表/详情页出现刚创建的记录"
    timeout_s: 1800
""")

        # 2c. 编辑流程
        if has_edit:
            cases.append(f"""  - name: "编辑流程"
    url: {base_url}
    instruction: |
      1. 用 {username} / {password} 登录
      2. 在列表页找到第一条记录，点击「编辑」
      3. 修改任意一个字段（比如改名称/改备注）
      4. 保存
      5. 确认编辑生效（字段值已更新）
    assertions:
      - "保存后编辑生效"
      - "没有报错"
    timeout_s: 1800
""")

        # 2d. 删除流程
        if has_delete:
            cases.append(f"""  - name: "删除流程"
    url: {base_url}
    instruction: |
      1. 用 {username} / {password} 登录
      2. 在列表页找到要删除的记录，点击「删除」
      3. 如果有确认弹窗，点击确认
      4. 确认删除成功（列表不再出现该记录）
    assertions:
      - "删除成功"
      - "列表页不再出现该记录"
    timeout_s: 1800
""")

    # 3. fallback——啥业务按钮都没探测到，给个模板
    if len(cases) == 1:
        cases.append("""  # ── TODO：补充业务流程 ──
  # init 没探测到明显的业务按钮（创建/编辑/删除等），
  # 可能需要先点进某个子菜单才能看到。手动补完后跑全量：
  #   python -m stagent run suite.yaml --config config.yaml
  #
  # - name: "完整业务流程"
  #   url: """ + f'"{base_url}"' + """
  #   instruction: |
  #     1. 登录
  #     2. 走一遍你们的核心业务链路
  #     3. 确认最终状态正确
  #   assertions:
  #     - "核心流程走通"
  #   timeout_s: 1800
""")

    return f"""# TestAgent 用例集（stagent init 自动生成）
name: {safe_name} 回归测试
app_url: "{base_url}"
standard_username: "{username}"
standard_password: "{password}"

cases:
{chr(10).join(cases)}"""


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
        _build_suite_yaml(base_url, username, password, post_cls, disc.get("sub_pages")), encoding="utf-8"
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
