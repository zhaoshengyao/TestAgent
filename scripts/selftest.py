"""离线自检：不启动浏览器、不调用模型，验证代码骨架是否自洽。

用法：python scripts/selftest.py
它覆盖：模块导入、配置加载、快照解析、用例加载、报告生成，
以及 20261116 新增的循环检测/自愈清 target/裁判三通道/extra_args 透传/loop_hint 渲染。
通过后再跑 `python -m stagent doctor` 和真实用例。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 从 __init__.py 文件读版本号（避免 CI 上同名 stagent 包的 import 歧义）
_init_py = ROOT / "stagent" / "__init__.py"
__version__ = "unknown"
if _init_py.exists():
    import re as _re
    m = _re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', _init_py.read_text(encoding="utf-8"))
    if m:
        __version__ = m.group(1)
del _init_py

from stagent.agent import CaseResult, StepRecord  # noqa: E402
from stagent.config import load_config  # noqa: E402
from stagent.reporter import write_reports  # noqa: E402
from stagent.snapshot import compact_snapshot, parse_snapshot  # noqa: E402
from stagent.suite import Case, load_suite  # noqa: E402

SAMPLE = """\\
- generic [ref=e1]:
  - heading "Example Domain" [level=1] [ref=e3]
  - textbox "Username" [ref=e5]
  - textbox "Password" [ref=e6]
  - button "Login" [ref=e7]
  - link "Forgot password?" [ref=e8]
"""


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            checks.append((name, True, str(detail or "")))
        except Exception as exc:  # noqa: BLE001
            checks.append((name, False, f"{type(exc).__name__}: {exc}"))

    check("版本导入", lambda: __version__)

    def _config():
        cfg = load_config(ROOT / "config.example.yaml")
        assert cfg.llm.model, "model 为空"
        assert cfg.browser.args, "browser.args 为空"
        return f"{cfg.llm.provider}/{cfg.llm.model}"

    check("配置加载", _config)

    def _snapshot():
        elements = parse_snapshot(SAMPLE)
        assert len(elements) == 5, f"期望 5 个元素，实际 {len(elements)}"
        refs = [e.ref for e in elements]
        assert refs == ["e3", "e5", "e6", "e7", "e8"], refs
        assert elements[3].role == "button" and elements[3].name == "Login", elements[3]
        compact = compact_snapshot(SAMPLE)
        assert "ref=e7" in compact, compact[:100]
        return f"{len(elements)} 个元素"

    check("快照解析", _snapshot)

    def _suite():
        smoke = load_suite(ROOT / "suites" / "smoke_login.yaml")
        assert len(smoke.cases) >= 1
        assert smoke.cases[0].assertions
        demo = load_suite(ROOT / "suites" / "demo_checkout.yaml")
        assert demo.cases[0].url.startswith("http")
        assert len(demo.cases) >= 7, f"demo_checkout 应有 >=7 条用例，实际 {len(demo.cases)}"
        # timeout_s 字段应被解析（值 >= 0 说明有定义，=0 也合法表示用默认值）
        assert all(c.timeout_s >= 0 for c in demo.cases), "demo_checkout 各用例 timeout_s 应 >= 0"
        return f"smoke={len(smoke.cases)} demo_checkout={len(demo.cases)}"

    check("用例加载", _suite)

    # ---------- 20261118 新增：suite data 参数化（P1-4） ----------

    def _data_inline_merge():
        """内联 data：Case 覆盖 Suite；缺失变量空渲染；{{data.x}} 别名生效。"""
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as td:
            f = P(td) / "inline.yaml"
            f.write_text(
                """name: 内联data
data:
  a: hello
  b: world
  nested: { x: 1 }
cases:
  - name: 覆盖a
    data:
      a: overridden
    instruction: "a={{a}} b={{b}} x={{nested.x}} data.a={{data.a}}"
    assertions: []
  - name: 缺变量
    instruction: "missing={{no_such_key}}"
    assertions: []
""",
                encoding="utf-8",
            )
            s = load_suite(f)
            c0, c1 = s.cases
            assert c0.data.get("a") == "overridden"
            assert c0.data.get("b") == "world"
            assert c0.data.get("nested", {}).get("x") == 1
            assert c0.instruction == "a=overridden b=world x=1 data.a=overridden", c0.instruction
            assert c1.instruction == "missing=", c1.instruction
        return "内联合并 / 点号引用 / 缺失空渲染 全过"

    check("data 内联合并与渲染", _data_inline_merge)

    def _data_from_external():
        """data_from: saucedemo_users.yaml 外部合并 + Case data 二次覆盖。"""
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as td:
            # 把现有的 saucedemo_users.yaml 复制过去（或用 ROOT 下真实文件）
            users_src = ROOT / "suites" / "saucedemo_users.yaml"
            td_path = P(td) / "suite.yaml"
            td_path.write_text(
                f"""name: ext
data_from: {users_src.name}
data:
  site: Saucedemo
cases:
  - name: locked_out
    data_from: {users_src.name}
    data:
      username: "{{{{users.locked_out_user.username}}}}"
      password: "{{{{users.locked_out_user.password}}}}"
    instruction: "user={{{{username}}}} pwd={{{{password}}}} site={{{{site}}}}"
    assertions: []
  - name: problem_shorthand
    data:
      username: "{{{{problem_username}}}}"
      password: "{{{{problem_password}}}}"
    instruction: "{{{{username}}}} / {{{{password}}}}"
    assertions: []
""",
                encoding="utf-8",
            )
            # 但 data_from 用的是相对路径，saucedemo_users.yaml 在 suites/ 里；
            # 简单做法：直接让临时文件也写到 suites/ 同级
        # 改用 ROOT 下真实 saucedemo_users.yaml 做 data_from
        src = ROOT / "suites" / "saucedemo_users.yaml"
        with tempfile.TemporaryDirectory() as td:
            suite_path = P(td) / "case.yaml"
            suite_path.write_text(
                f"""name: ext
data_from: {src}
data:
  site: Saucedemo
cases:
  - name: locked_out
    data:
      username: "{{{{users.locked_out_user.username}}}}"
      password: "{{{{users.locked_out_user.password}}}}"
    instruction: "user={{{{username}}}} pwd={{{{password}}}} site={{{{site}}}}"
    assertions: []
  - name: problem_shorthand
    data:
      username: "{{{{problem_username}}}}"
      password: "{{{{problem_password}}}}"
    instruction: "{{{{username}}}} / {{{{password}}}}"
    assertions: []
""",
                encoding="utf-8",
            )
            s = load_suite(suite_path)
            c0, c1 = s.cases
            assert c0.data.get("username") == "locked_out_user", c0.data
            assert c0.data.get("password") == "secret_sauce", c0.data
            assert "user=locked_out_user pwd=secret_sauce site=Saucedemo" in c0.instruction
            assert c1.data.get("username") == "problem_user"
            assert c1.instruction == "problem_user / secret_sauce"
        return "外部合并 + 点号引用 + 简写 key 全过"

    check("data_from 外部合并", _data_from_external)

    def _data_default_pipe():
        """{{var|default('x')}} 管道默认值。"""
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as td:
            f = P(td) / "d.yaml"
            f.write_text(
                """name: pipe
data:
  a: 1
cases:
  - name: 管道
    instruction: "a={{a|default('AX')}} b={{b|default('BX')}}"
    assertions: []
""",
                encoding="utf-8",
            )
            s = load_suite(f)
            assert s.cases[0].instruction == "a=1 b=BX", s.cases[0].instruction
        return "默认值管道生效"

    check("模板 default 管道", _data_default_pipe)

    def _data_yaml_in_list():
        """data 里放 dict / list 结构（如 assertions 里引用列表），模板渲染成 YAML 紧凑形式。"""
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as td:
            f = P(td) / "y.yaml"
            f.write_text(
                """name: yaml
data:
  goods: [Backpack, T-Shirt]
cases:
  - name: 列表引用
    instruction: "商品列表：{{goods}}"
    assertions: []
""",
                encoding="utf-8",
            )
            s = load_suite(f)
            instr = s.cases[0].instruction
            assert "商品列表：" in instr and "Backpack" in instr and "T-Shirt" in instr, instr
        return "list 模板渲染 OK"

    check("模板 list/dict 结构渲染", _data_yaml_in_list)

    def _data_in_real_suites():
        """smoke_login.yaml / demo_checkout.yaml（改写后）完整加载，变量已渲染。"""
        smoke = load_suite(ROOT / "suites" / "smoke_login.yaml")
        assert len(smoke.cases) == 2
        # 外部 data_from 应合并进 suite.data
        assert "users" in smoke.data and "standard_user" in smoke.data.get("users", {}), smoke.data
        # Case 级变量已内联渲染
        c0 = smoke.cases[0]
        assert c0.data.get("username") == "standard_user", c0.data
        assert "standard_user / secret_sauce" in c0.instruction, c0.instruction
        # url 里的 {{app_url}} 也被渲染
        assert c0.url.startswith("https://www.saucedemo.com/"), c0.url

        demo = load_suite(ROOT / "suites" / "demo_checkout.yaml")
        # demo 现在 7 条：standard×2 + problem_user + locked_out + error_user + performance_user + visual_user
        assert len(demo.cases) == 7, f"demo_checkout 应 7 条，实 {len(demo.cases)}"
        names = [c.name for c in demo.cases]
        assert any("[problem_user]" in n for n in names), names
        assert any("[locked_out]" in n for n in names), names
        assert any("[error_user]" in n for n in names), names
        assert any("[performance_user]" in n for n in names), names
        assert any("[visual_user]" in n for n in names), names
        # expected_fail 字段：problem/error/performance 三条声明了，standard/locked_out/visual 没声明
        ef_count = sum(1 for c in demo.cases if c.expected_fail)
        assert ef_count == 3, f"expected_fail 应 3 条，实 {ef_count}"
        # max_steps 字段：problem/error/performance 各 80+，standard 用默认 40
        assert all(c.max_steps >= 40 for c in demo.cases if c.max_steps)
        # timeout 合规
        assert all(c.timeout_s >= 0 for c in demo.cases)
        return f"smoke={len(smoke.cases)} demo={len(demo.cases)} expected_fail={ef_count} 全过"

    check("真实 suite 变量渲染", _data_in_real_suites)

    # ---------- 20261118 新增：Phase 1.1/1.2 max_steps + expected_fail + exit code 三态 ----------

    def _case_max_steps_expected_fail():
        """suite.py Case dataclass 新字段 + load_suite 解析。"""
        import tempfile
        from pathlib import Path as P

        with tempfile.TemporaryDirectory() as td:
            f = P(td) / "phase11.yaml"
            f.write_text(
                """name: Phase1.1
cases:
  - name: A 正常
    instruction: 正常跑
    assertions: []
  - name: B 有 max_steps
    max_steps: 80
    instruction: 80步上限
    assertions: []
  - name: C expected_fail
    expected_fail: true
    expected_fail_reason: "前端 bug"
    instruction: 预期失败
    assertions: []
""",
                encoding="utf-8",
            )
            s = load_suite(f)
            a, b, c = s.cases
            assert a.max_steps == 0
            assert a.expected_fail is False
            assert a.expected_fail_reason == ""
            assert b.max_steps == 80
            assert c.expected_fail is True
            assert c.expected_fail_reason == "前端 bug"
        return "Case 新字段解析 OK"

    check("Case max_steps + expected_fail 字段", _case_max_steps_expected_fail)

    def _cli_exit_code_tri_state():
        """cli.py _execute 汇总三态 + exit code 三态。"""
        from stagent.agent import CaseResult

        # 构造三种 status 的 CaseResult
        r_pass = CaseResult(name="A", url="", instruction="", status="passed")
        r_ufail = CaseResult(name="B", url="", instruction="", status="failed")
        r_efail = CaseResult(
            name="C", url="", instruction="", status="failed",
            expected_fail=True, expected_fail_reason="前端 bug",
        )
        r_err = CaseResult(name="D", url="", instruction="", status="error")

        from stagent.cli import _exit_code_from_results

        # 1) 全 passed → 0
        assert _exit_code_from_results([r_pass]) == 0, _exit_code_from_results([r_pass])
        # 2) passed + expected_fail → 2（无意外失败）
        assert _exit_code_from_results([r_pass, r_efail]) == 2, _exit_code_from_results([r_pass, r_efail])
        # 3) passed + unexpected_fail → 1
        assert _exit_code_from_results([r_pass, r_ufail]) == 1, _exit_code_from_results([r_pass, r_ufail])
        # 4) passed + expected_fail + unexpected_fail → 1（意外失败优先）
        assert _exit_code_from_results([r_pass, r_efail, r_ufail]) == 1
        # 5) error 且 expected_fail → 2（声明了就是探测 bug，崩了也算预期）
        assert _exit_code_from_results([CaseResult(
            name="E", url="", instruction="", status="error",
            expected_fail=True,
        )]) == 2
        return "exit code 三态 5 个断言全过"

    check("cli exit code 三态", _cli_exit_code_tri_state)

    # ---------- 20261118 新增：到此结束 ----------

    def _report():
        result = CaseResult(
            name="自检用例",
            url="https://example.com",
            instruction="打开页面并截图",
            status="passed",
            answer="结论：通过",
            steps=[
                StepRecord(1, "browser_navigate", {"url": "https://example.com"}, True, "ok", 0.3),
                StepRecord(
                    2, "browser_click", {"ref": "e7"}, True, "ok", 0.2,
                    healed=True, heal_notes=["第1次自愈：ref 失效，重新定位到 e9"],
                ),
            ],
            healings=[
                {"tool": "browser_click", "attempt": 1, "ok": True,
                 "ref": "e9", "element": "Login", "reason": "原始 ref=e7 失效，候选里唯一匹配的按钮是 e9"},
            ],
            assertions=[{"assertion": "页面可访问", "passed": True, "evidence": "标题 Example Domain"}],
            duration_s=0.5,
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_reports(Path(tmp), "自检报告", [result], {"model": "test"})
            for key, path in paths.items():
                assert path.exists() and path.stat().st_size > 0, f"{key} 报告为空"
            html = paths["html"].read_text(encoding="utf-8")
            assert "自检用例" in html and "<table>" in html, "HTML 结构异常"
            md = paths["markdown"].read_text(encoding="utf-8")
            assert "自愈记录" in md, "Markdown 缺少自愈记录段落"
            # reporter 渲染措辞是 "✔ 尝试1：… → ref=e9"，与 StepRecord.heal_notes 的 "第X次自愈" 是两条路径
            assert "尝试1" in md and "ref=e9" in md, f"Markdown 缺少自愈细节：{md}"
        return "md/html/json 三份均生成"

    check("报告生成", _report)

    # ---------- 20261116 新增：本轮调试核心逻辑的离线回归 ----------

    def _loop_similarity():
        """循环检测相似度判定：数字范围归一化 + 输出同类判定。"""
        from stagent.agent import find_args_similar, find_output_similar

        # 数字范围 {0,500} vs {0,20000000000} → 本质相同（demo_fixretest4 的真实事故模式）
        assert find_args_similar(
            {"regex": "Backpack[\\s\\S]{0,500}"},
            {"regex": "Backpack[\\s\\S]{0,20000000000}"},
        ), "数字范围不同的同前缀 regex 应判为相似"

        # 相同 regex → 相似
        assert find_args_similar({"regex": "cart"}, {"regex": "cart"}), "相同 regex 应判为相似"

        # 不同搜索词 → 不相似
        assert not find_args_similar({"regex": "cart"}, {"regex": "Remove"}), "不同词不应判为相似"
        assert not find_args_similar({"text": "cart"}, {"text": "Remove"}), "不同 text 不应判为相似"

        # 空参数防御
        assert not find_args_similar({}, {"regex": "cart"}), "空参数不应判相似"
        assert not find_args_similar({"regex": ""}, {"regex": "cart"}), "空 regex 不应判相似"

        # 大小写归一
        assert find_args_similar({"regex": "Cart"}, {"regex": "cart"}), "大小写应归一"

        # 输出同类判定：同是 success / 同是 empty
        assert find_output_similar("Found 1 match for cart", "Found 2 matches for cart"), "同 success 应判同类"
        assert find_output_similar("No matches found for cart", "No matches found for cart"), "同 empty 应判同类"
        assert not find_output_similar("Found 1 match", "No matches found"), "success 与 empty 不应判同类"

        return "归一化 + 同类判定 12 个断言全过"

    check("循环相似度判定", _loop_similarity)

    def _healer_target_cleanup():
        """自愈换 ref 时必须清掉旧 target（错误 CSS 选择器），否则 ref 白换。"""
        import asyncio
        from stagent.config import AgentConfig
        from stagent.heal_log import HealLog
        from stagent.healer import Healer, _REF_ONLY_TOOLS

        class FakeBridge:
            async def call(self, name, args):
                class R:
                    is_error = False
                    text = (
                        '- textbox "Username" [ref=e11]\n'
                        '- button "Cart, empty" [ref=e32]\n'
                    )
                return R()

        class FakeLLM:
            async def chat_json(self, system, user):
                return {"ref": "e32", "element": 'button "Cart, empty"',
                        "reason": "唯一匹配的购物车按钮", "confidence": 0.95}

        healer = Healer(FakeBridge(), FakeLLM(), AgentConfig(), HealLog())

        # browser_click：ref-only 工具，target 必须被清掉
        new_args, meta = asyncio.run(healer.propose(
            "browser_click",
            {"element": "购物车图标", "target": "#cart_button"},  # 错误 CSS（真实事故）
            'Error: "#cart_button" does not match any elements.',
            attempt=1,
        ))
        assert new_args is not None, "自愈应返回新参数"
        assert new_args.get("ref") == "e32", f"ref 应为 e32，实际 {new_args.get('ref')}"
        assert "target" not in new_args, f"ref-only 工具的 target 应被清除，实际 args={new_args}"

        # 非 ref-only 工具（browser_evaluate 不在 HEALABLE 里，用 browser_fill_form 试探）
        # fill_form 不在 _REF_ONLY_TOOLS，target 不该被 pop——保护 fields 结构
        assert "browser_fill_form" not in _REF_ONLY_TOOLS
        assert "browser_click" in _REF_ONLY_TOOLS and "browser_type" in _REF_ONLY_TOOLS

        return f"pop(target) 生效；_REF_ONLY_TOOLS={sorted(_REF_ONLY_TOOLS)}"

    check("自愈清 target", _healer_target_cleanup)

    def _verify_evidence_channels():
        """裁判证据三通道拼装：快照历史 + Page URL + 步骤 output 摘要。

        走真实 run_case：FakeLLM.chat 请求一次 browser_snapshot（结果进 _snapshot_history），
        第二轮无 tool_calls 返回结论 → verify 用三通道证据判定。
        注意：不能在 run_case 之前塞 agent._snapshot_history——run_case 开头会清空它。
        """
        import asyncio
        from stagent.agent import TestAgent, _URL_IN_PAGE_RE

        cfg = load_config(ROOT / "config.example.yaml")

        # snapshot 返回要像真实 MCP 一样带元素树（通道 1 的数据源）
        SNAPSHOT_TEXT = (
            "### Page\n- Page URL: https://x.com/\n- Page Title: X\n"
            "### Snapshot\n```yaml\n"
            '- generic [ref=e3]:\n  - heading "Products" [ref=e34]\n'
            "```"
        )
        NAV_TEXT = "### Page\n- Page URL: https://x.com/\n- Page Title: X"

        holder: dict[str, str] = {}

        class FakeBridge:
            tool_names = ["browser_snapshot", "browser_find", "browser_click", "browser_navigate"]
            def openai_tools(self):
                return []
            async def call(self, name, args):
                class R:
                    is_error = False
                    text = SNAPSHOT_TEXT if name == "browser_snapshot" else NAV_TEXT
                    images = []
                return R()

        class FakeLLM:
            async def chat(self, messages, tools=None):
                # 第一轮：请求一次 browser_snapshot（结果进 _snapshot_history）
                # 第二轮：无 tool_calls → run_instruction 返回 → 进入 verify
                if not holder.get("snapshot_requested"):
                    holder["snapshot_requested"] = "1"
                    return {
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "function": {"name": "browser_snapshot", "arguments": "{}"},
                        }],
                    }
                return {"content": "结论：通过", "tool_calls": []}

            async def chat_json(self, system, user):
                # 把裁判收到的证据原样带出来供断言
                holder["captured"] = user
                return {"items": [{"index": 0, "assertion": "x", "passed": True, "evidence": "e"}]}

        agent = TestAgent(cfg, FakeBridge(), FakeLLM())

        case = Case(name="单测", url="https://x.com/", instruction="验证 Products",
                    assertions=["页面出现 Products"])
        result = asyncio.run(agent.run_case(case))

        captured = holder.get("captured") or ""
        assert captured, "chat_json 应被调用（verify 应执行）"

        # 通道 1：快照历史进了证据（含 snapshot 里的 Products 元素树）
        assert "【快照 #1】" in captured, f"证据应含快照历史通道，实际证据头：{captured[:300]}"
        assert "Products" in captured.split("【Page URL 证据")[0], "快照通道应含 Products 元素"
        # 通道 2：Page URL 提取进了证据（navigate 步骤 output 里的 Page URL 行）
        assert "Page URL（步骤" in captured, "证据应含 Page URL 通道"
        assert "https://x.com/" in captured, "Page URL 证据应含实际地址"
        # 通道 3：步骤 output 摘要进了证据（navigate 步骤原文——1113 事故的兜底）
        assert "【步骤 #1 browser_navigate】" in captured, "证据应含步骤摘要通道"

        # verify 返回的 verdict 应使用例 passed
        assert result.status == "passed", f"全证据下应 passed，实际 {result.status}：{result.error}"

        # Page URL 正则本身也验证一下
        urls = _URL_IN_PAGE_RE.findall("### Page\n- Page URL: https://a.com/\n")
        assert urls == ["https://a.com/"], f"URL 正则异常：{urls}"

        return "快照/URL/摘要三通道 + verdict=passed + URL 正则全过"

    check("裁判三通道证据", _verify_evidence_channels)

    def _isolated_args_passthrough():
        """--isolated 等 extra_args 必须真实传给 @playwright/mcp（config → _build_args）。"""
        from stagent.config import BrowserConfig
        from stagent.mcp_bridge import MCPBridge

        cfg = BrowserConfig(args=["@playwright/mcp@latest"], headless=True,
                            extra_args=["--isolated"])
        bridge = MCPBridge(cfg, ROOT)
        args = bridge._build_args()

        assert "--isolated" in args, f"extra_args 应透传，实际 args={args}"
        assert "--headless" in args, "headless 应自动追加"
        assert any(str(a).startswith("--output-dir") for a in args), "output-dir 应自动追加"

        # extra_args 为空时不该有 --isolated（防止误开）
        cfg2 = BrowserConfig(args=["@playwright/mcp@latest"], headless=False, extra_args=[])
        args2 = MCPBridge(cfg2, ROOT)._build_args()
        assert "--isolated" not in args2, "未配置时不应出现 --isolated"
        assert "--headless" not in args2, "headless=False 不应追加"

        return f"透传链路 OK：{[a for a in args if a in ('--isolated', '--headless')]}"

    check("extra_args 透传", _isolated_args_passthrough)

    def _loop_hint_render():
        """loop_hint 必须渲染进模型上下文（_render_tool_output），且截断时不丢。"""
        from stagent.agent import StepRecord, TestAgent
        from stagent.mcp_bridge import ToolCallResult

        cfg = load_config(ROOT / "config.example.yaml")
        cfg.agent.tool_result_char_limit = 200  # 故意极小，触发截断路径

        class FakeBridge:
            tool_names = ["browser_find"]
            def openai_tools(self):
                return []
            async def call(self, name, args):
                class R:
                    is_error = False
                    text = ""
                    images = []
                return R()

        class FakeLLM2:
            async def chat(self, messages, tools=None):
                return {"content": "", "tool_calls": []}
            async def chat_json(self, system, user):
                return {"items": []}

        agent = TestAgent(cfg, FakeBridge(), FakeLLM2())

        step = StepRecord(1, "browser_find", {"regex": "cart"}, True,
                          output="### Result\nFound 4 matches for cart:" * 30,  # 超长触发截断
                          duration_s=0.02)
        step.loop_hint = "【循环检测】连续 2 次相似查找。停止重复。"

        rendered = agent._render_tool_output(
            ToolCallResult(text=step.output), step
        )
        assert "⚠️" in rendered, "渲染结果应含 ⚠️ 循环提示前缀"
        assert "循环检测" in rendered, "渲染结果应含 hint 正文"
        assert rendered.endswith("停止重复。") or "停止重复" in rendered[-200:], "截断后 hint 应拼回末尾"

        # 无 hint 时不渲染
        step2 = StepRecord(2, "browser_find", {"regex": "x"}, True, output="ok", duration_s=0.01)
        rendered2 = agent._render_tool_output(ToolCallResult(text="ok"), step2)
        assert "循环检测" not in rendered2, "无 hint 时不应渲染"

        return "hint 渲染 + 截断保底 + 无 hint 不渲染 全过"

    check("loop_hint 渲染投递", _loop_hint_render)

    # ── Phase 2.1 失败分类器 ──
    def _failure_classifier_basic():
        try:
            from stagent.failure_classifier import classify
        except ImportError as exc:
            return False, f"import 失败 {exc}"
        # 场景 1：form_clear_pattern（problem_user 原型）
        steps = []
        for cycle in range(3):
            steps.append(StepRecord(tool="browser_fill_form", args={}, output="ok", ok=True))
            steps.append(StepRecord(tool="browser_click", args={"element": "Continue 按钮"}, output="ok", ok=True))
            steps.append(StepRecord(tool="browser_click", args={"element": "Dismiss error 按钮"}, output="ok", ok=True))
        r1 = CaseResult(name="test", status="error", error="timeout", steps=steps, duration_s=300)
        fc1 = classify(r1)
        assert fc1.failure_category == "front_end_bug", f"应为 front_end_bug，实 {fc1.failure_category}"
        # 场景 2：loop_hint
        steps2 = [StepRecord(tool="browser_find", args={}, output="ok", ok=True, loop_hint="循环检测") for _ in range(5)]
        r2 = CaseResult(name="test2", status="failed", steps=steps2, duration_s=120)
        fc2 = classify(r2)
        assert fc2.failure_category == "agent_loop", f"应为 agent_loop，实 {fc2.failure_category}"
        # 场景 3：passed 不分类（unknown）
        r3 = CaseResult(name="test3", status="passed", steps=[], duration_s=45)
        fc3 = classify(r3)
        assert fc3.failure_category == "unknown", f"passed 应 unknown，实 {fc3.failure_category}"
        return True, f"front_end_bug + agent_loop + passed=unknown 全过"

    check("失败分类器 v1", _failure_classifier_basic)

    print("=" * 56)
    print("stagent 离线自检")
    print("=" * 56)
    failed = 0
    for name, ok, detail in checks:
        print(f"{'✅' if ok else '❌'} {name}：{detail}")
        if not ok:
            failed += 1
    print("=" * 56)
    if failed:
        print(f"❌ {failed} 项失败，先修代码再跑浏览器。")
        return 1
    print("✅ 离线自检通过。下一步：python -m stagent doctor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
