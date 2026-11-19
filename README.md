# stagent —— 一句话跑通 Web UI 回归的测试智能体

[![CI](https://github.com/zhaoshengyao/TestAgent/actions/workflows/regression.yml/badge.svg)](https://github.com/zhaoshengyao/TestAgent/actions)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

不写选择器、不写等待时间、不写 Page Object。一句话指令 → LLM 决策 → Playwright MCP 工具 → 真实浏览器 → 页面快照回灌 → 下一步决策，失败时自动做选择器自愈 + 失败根因自动分类，跑完出带截图的 Markdown/HTML/JSON 三合一报告。

## 和传统 Playwright 脚本的区别

| | 传统脚本 | 本 Agent |
| --- | --- | --- |
| 输入 | `click('#submit-btn')` | "用演示账号登录并确认进入商品列表" |
| 定位 | CSS/XPath，前端改 class 就挂 | 快照里的可读元素（role + 名称），挂了自动重新找 |
| 维护 | 每次 UI 变更改脚本 | 改指令或改断言 |
| 上下文 | 无 | 每一步 DOM/URL/报错都回灌给模型 |
| 产物 | 断言日志 | 截图 + 步骤流水 + 自愈记录 + 断言证据 |

## 快速开始

### 前置

- Python **3.10+**、Node.js **16+**（`npx` 可用）
- 模型：本地 Ollama `qwen3:latest`（推荐），或任意 OpenAI 兼容服务

### 安装

```powershell
# 手动等价步骤
pip install -r requirements.txt
copy config.example.yaml config.yaml
copy .env.example .env
npx -y playwright install chromium
python scripts/selftest.py          # 离线自检（不启动浏览器）
python -m stagent doctor             # 环境/MCP 自检
```

### 跑真实用例

```bash
python -m stagent run suites/smoke_login.yaml

# 或一句话直接跑
python -m stagent ask "打开 https://www.saucedemo.com/ 用演示账号登录，确认进入商品列表，每步截图"
```

## 实际跑通的报告示例

以下是 2026-11-13 冒烟用例（修复断言裁判证据链后）的完整产物：

**运行摘要**：

| # | 用例 | 结果 | 关键断言证据 |
|---|------|------|-------------|
| 1 | 演示账号登录 | ✅ passed (172.9s, 11 steps) | URL 跳转 `/inventory.html`；`browser_find` 找到 `Products` 标题 |
| 2 | 错误密码应被拒绝 | ✅ passed (159.4s, 10 steps) | 页面出现 `Epic sadface: Username and password do not match`；URL 仍在登录页 |

**完整报告**（带截图缩略图 + 每步详情）：
- [HTML 报告](artifacts/runs/fixverify_retest/report.html)
- [Markdown 报告](artifacts/runs/fixverify_retest/report.md)
- [JSON 数据](artifacts/runs/fixverify_retest/summary.json)

---

## 用例怎么写

就三件事：`name` + `instruction`（自然语言步骤） + `assertions`（可验证的事实）。

```yaml
# suites/smoke_login.yaml（实际跑通的用例）
name: 登录冒烟
base_url: https://www.saucedemo.com/

cases:
  - name: 演示账号登录
    url: https://www.saucedemo.com/
    instruction: |
      1. 打开 https://www.saucedemo.com/
      2. 观察页面，确认用户名/密码输入框和登录按钮
      3. 从页面文本里读出演示账号（standard_user / secret_sauce）
      4. 填入并登录，成功后截图
    assertions:
      - "登录后 URL 包含 /inventory.html"
      - "登录后页面出现商品列表（Products 标题或商品条目）"
    timeout_s: 300
```

要跑真实完整链路的话，看 `suites/demo_checkout.yaml`（公开 Saucedemo 站点，直接跑就能看到购物车→结算→下单全链路）。

### 断言裁判的工作方式

执行完后，Agent 会**独立发起第二个 LLM 请求**做"裁判"，逐条判定 `assertions` 是否成立。裁判不依赖 Agent 的自评，只看三条客观证据：

1. **Page URL**（硬证据，来自 `browser_navigate` / `browser_click` 的自动回灌）
2. **页面快照历史**（`auto_observe: true` 时每次动作后自动补 snapshot）
3. **工具 output 原文摘要**（兜底：`browser_find` 的匹配结果、元素树片段等）

任何一条能佐证，就应该判 `passed`。证据链完整 = 不会误判。

## 配置

```yaml
# config.yaml（修改自己的模型地址即可）
llm:
  provider: custom
  model: Qwen3.8-27B-FP8
  base_url: http://192.168.2.10:3000/v1   # 本地部署
  temperature: 0.2

agent:
  auto_observe: true           # ← 关键！动作后自动快照补证据
  screenshot_each_step: true   # 关键动作自动截图
  heal_attempts: 2             # 选择器自愈次数
  max_steps: 40                # 单条用例最大步数
```

## 三个核心机制

**1. 快照代替选择器**
`browser_snapshot` 返回可访问性树文本（`button "登录" [ref=e12]`），角色和可读名称比 CSS 类名稳定得多。

**2. 自愈定位**
点击/输入失败时：重新快照 → 模型看「原始意图 + 报错 + 当前所有可交互元素」→ 返回替代 `ref` → 重试。自愈只改"怎么找元素"，不改"断言什么"。

**3. 断言与执行分离**
模型说"成功了"不算数。裁判只认页面证据——URL、快照里的元素、工具 output 里的匹配结果。

## 产物去哪了

```
artifacts/runs/<时间戳>/
├─ report.md       # 人类可读报告（步骤表 + 断言 + Agent 结论）
├─ report.html     # 带截图缩略图的浏览器版报告
├─ summary.json    # 结构化数据（流水线/CICD 读）
└─ 原始截图 *.png  # Agent 主动截的 + 关键动作自动截的
```

退出码：`0` 全部通过 | `2` 有 expected_fail（known bug，CI 绿）| `1` 有 unexpected_fail 或 error（CI 红，阻断）

## 失败自动分类 v1

不是只会说"❌"——失败了自动告诉你根因，写进 `summary.json` 和报告：

| 分类 | 含义 | 信号 |
|---|---|---|
| **front_end_bug** | 页面自身 bug | fill_form 值被清 / run_code_unsafe 死循环 |
| **agent_loop** | Agent 陷入循环 | loop_hint 连续触发 |
| **agent_timeout** | timeout_s 杀停 | error 含 "timeout" |
| **agent_max_steps** | max_steps 杀停 | answer 含 "达到最大步数" |
| **assertion_judge** | 裁判说断言语义不匹配 | 断言 passed=False 且 status=failed |
| **agent_strategy** | 工具 error + 自愈失败 | tool_error>0 且 healer 没救成 |
| **unknown** | 都不命中 | — |

```json
{
  "failure_category": "front_end_bug",
  "confidence": 0.92,
  "reason": "检测到 Checkout 表单值被前端 bug 反复清掉（fill_form→Continue→dismiss 模式出现 3 次）"
}
```

纯规则引擎，不调 LLM，毫秒级，可复现。

## GitHub Actions CI

仓库 → Settings → Secrets → Actions → New secret：

| Name | Value |
|---|---|
| `DEEPSEEK_API_KEY` | `sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |

| 事件 | Job | 耗时 | 触发 |
|---|---|---|---|
| PR / push main | `smoke` | ~5min | `suites/smoke_login.yaml` |
| 每天 UTC 22:00 | `nightly` | ~30min | `suites/demo_checkout.yaml`（7 条全量） |

CI 退出码翻译（`scripts/ci_run.py`）：CLI `0` / `2` → GitHub Actions 绿，CLI `1` → 红（阻断）。

Artifacts 自动上传：smoke 保留 7 天，nightly 保留 30 天。

## 目录结构

```
TestAgent/
├─ stagent/                  # 核心包
│  ├─ agent.py                 # TestAgent + CaseResult + 滑动窗口循环检测
│  ├─ cli.py                   # CLI 三标记 ✅⚠️❌💥 + exit code 三态
│  ├─ config.py                # 配置加载（yaml → 环境变量 → preset）
│  ├─ failure_classifier.py    # ★ 失败自动分类器 v1（规则引擎，不调 LLM）
│  ├─ healer.py                # 选择器自愈定位（+ 框架级策略重试 fill_form→type）
│  ├─ llm.py                   # AsyncOpenAI + 429 指数退避
│  ├─ mcp_bridge.py            # @playwright/mcp stdio 协议桥接
│  ├─ reporter.py              # md/html/json 报告生成
│  ├─ suite.py                 # Case dataclass + YAML 解析
│  ├─ snapshot.py              # Playwright accessibility tree 解析
│  └─ doctor.py                # 环境自检
├─ suites/                    # 用例集
│  ├─ demo_checkout.yaml       # 7 条全量（Saucedemo 5 账号 × 购物车→结算）
│  └─ smoke_login.yaml         # 2 条冒烟
├─ scripts/
│  ├─ selftest.py              # 离线自检（20+ 断言，覆盖分类器）
│  ├─ ci_run.py                # CI exit code 翻译（0/2→pass, 1→fail）
│  └─ test_classifier.py       # 分类器 6 场景单测
├─ .github/workflows/
│  └─ regression.yml           # GitHub Actions CI（smoke PR + nightly 全量）
├─ config.example.yaml         # 本地模板 → config.yaml
├─ config.ci.example.yaml     # CI 专用（provider=deepseek, 节流最小, headless）
├─ Dockerfile                  # Playwright 官方 Python 镜像
├─ requirements.txt             # 只有 3 个第三方包！pyyaml + openai + mcp
├─ .gitignore                  # 防 config.yaml / artifacts / __pycache__ 提交
├─ docs/                       # 交付文档
└─ README.md
```

## 边界

- 探索性测试、视觉审美判断、复杂 Canvas 交互 —— 仍需人工
- 强合规/资金类操作 —— 只建议在沙箱跑，且人工复核报告
- 大规模 DOM 重构 —— 自愈覆盖不到，需人工改指令

## 常见问题

**Q: 模型老是点错元素？**
`config.yaml` 里 `agent.auto_observe: true`（默认）已经开了。也可以换模型——工具调用更稳的模型效果更好。

**Q: 为什么断言全过了？Agent 自评不算数吗？**
不算。Agent 自己说"登录成功"没用，裁判必须看到三条客观证据之一：URL 真跳了 / 快照里真有 Products / `browser_find` 真找到了。

**Q: 能不能不用本地 Ollama？**
可以。把 `config.yaml` 改成任意 OpenAI 兼容服务的 `provider`/`base_url`/`api_key` 即可。

**Q: 无头模式看不到过程？**
`config.yaml` 里 `browser.headless: false`，就能看到浏览器真实操作。

**Q: 首次运行很慢？**
`npx @playwright/mcp@latest` 首次要拉包。可以先手动 `npx -y @playwright/mcp@latest --help` 预热一下。
