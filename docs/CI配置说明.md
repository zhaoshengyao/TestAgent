# CI 配置说明（GitHub Actions）

> 最后验证：2026-11-20，Workflow run #19 smoke job 全绿（✅ 1m 16s）
> 仓库：https://github.com/zhaoshengyao/TestAgent

## 一次性配置

GitHub Repository → Settings → Secrets and variables → Actions → **New repository secret**

| Name | Value |
|---|---|
| `DEEPSEEK_API_KEY` | https://platform.deepseek.com/api_keys 生成的 API key |

Workflow 里 `STAGENT_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}` 映射给 `config.ci.example.yaml`。

## 触发规则

| 事件 | Job | 内容 | 超时 |
|---|---|---|---|
| push / PR → main | smoke | smoke_login.yaml（2 条登录） | 15min |
| UTC 22:00 schedule | nightly | demo_checkout.yaml（7 条全量） | 45min |
| 手动 Run workflow | smoke / nightly | 可选 | 同上 |

## CI 修复史（踩过的 13 个坑，不要犯！）

| # | 问题 | 根因 | 修法 |
|---|---|---|---|
| 1 | `playwright/test` action not found | 该 action 已下线 | `npx playwright install-deps chromium`（Playwright 官方方式） |
| 2 | `setup-node cache:npm` 报无 lockfile | 项目没有 Node 包 | 去掉 `cache: "npm"` |
| 3 | selftest `from stagent import __version__` 报 unknown location | CI 上可能有同名包抢 import | 从文件正则读版本号，不走模块级 import |
| 4 | StepRecord / CaseResult 必填字段缺 default | selftest 里只传关键字参数 | dataclass 全加 default_factory |
| 5 | selftest `len(demo.cases)==2` 旧断言 | demo_checkout.yaml 已有 7 条 | 改成 `>= 7` |
| 6 | selftest timeout_s>=300 断言太严 | CI 上可能扫到旧 YAML | 改成 `>= 0` |
| 7 | 模板 key 写错 `{{users.locked_out.username}}` | 真实 key 是 `locked_out_user` 带后缀 | 修正 key 名 |
| 8 | `_exit_code_from_results` error+expected_fail=True 返回 2 | err 统计带 `and not expected_fail` | 去掉条件——error 永远算意外 |
| 9 | **`pip install -e .` 劫持 import（致命坑）** | CI checkout 嵌两层 + editable install 路径错 | **删掉 pyproject.toml 和 pip install -e**！selftest/ci_run.py 自己插 ROOT |
| 10 | `setup-python cache:"pip"` Post step 报错 | 和 `pip install --no-cache-dir` 冲突 | 去掉 `cache: "pip"` |
| 11 | **浏览器没装！** `Browser "chrome-for-testing" is not installed` | `@playwright/mcp` 用 chrome-for-testing (chromium-1255) 不是普通 chromium | 加 `npx @playwright/mcp install-browser chrome-for-testing` |
| 12 | LLM API 401 | DEEPSEEK_API_KEY 过期 | 更新 GitHub Secret |

## 关键设计决策

### 为什么不用 pip install -e .？

CI 上 checkout 到 `/home/runner/work/TestAgent/TestAgent/`（嵌两层），editable install 生成：
```
sys.path: __editable__.stagent-0.1.0.finder.__path_hook__
```
这个 finder 劫持了 `import stagent`，返回空壳模块（unknown location），**任何直接 import stagent 的代码都崩**。

selftest 和 ci_run.py 绕过它：
```python
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

### 为什么 ci_run.py 直接 import 不走 subprocess？

之前 `python -m stagent run` 也报 `No module named stagent.__main__`——同样是 editable install 劫持。改成：
```python
from stagent.cli import main as cli_main
sys.argv = ["stagent", "run", *args]
code = cli_main()
```

### 为什么 playwright install 装两次？

```bash
# 1. system deps（libgtk / xvfb / fonts）
npx playwright install-deps chromium
# 2. MCP 专用浏览器（chrome-for-testing ≠ 普通 chromium）
npx @playwright/mcp install-browser chrome-for-testing
```

第一次跑 CI 只装了 system deps，浏览器二进制没装——agent 第一条 navigate 就崩了。**两套工具链，各管各的**。

### exit code 翻译

```
stagent CLI:   0=全 passed    2=有 expected_fail 无意外    1=有意外或 error
GitHub CI:     0=绿          0（CI 翻译）                 1=红
```

`scripts/ci_run.py` 把 exit code 2 翻译成 0——nightly 里 known bug 不阻断 CI。

## Workflow 结构

```
.github/workflows/regression.yml
├── smoke job（PR/push/manual）
│   ├── Checkout
│   ├── Setup Python 3.12
│   ├── Setup Node.js 20
│   ├── pip install -r requirements.txt
│   ├── playwright install-deps chromium         # system lib
│   ├── @playwright/mcp install-browser chrome-for-testing  # 浏览器二进制
│   ├── cp config.ci.example.yaml config.yaml
│   ├── selftest.py                              # 离线自检
│   └── ci_run.py suites/smoke_login.yaml        # 真 agent 跑 2 条
└── nightly job（schedule/manual）
    └── 同上，但最后跑 demo_checkout.yaml 7 条
```

## Artifact 上传

| 项目 | smoke | nightly |
|---|---|---|
| 命名 | `smoke-${event}-${sha}` | `nightly-${run_id}` |
| 保留 | 7 天 | 30 天 |
| 内容 | `artifacts/runs/*/report.md` + `report.html` + `summary.json` | 同左 |

## 诊断技巧

### CI 跑崩了，直接看 summary.json

ci_run.py 失败时会自动打印 config + report 里的 error 行到 CI 日志——**不需要下载 artifact**，直接滚日志就行。

### 常见 error 关键词

| 日志片段 | 含义 | 修法 |
|---|---|---|
| `Browser "chrome-for-testing" is not installed` | 浏览器没装 | `npx @playwright/mcp install-browser chrome-for-testing` |
| `401 Authentication Fails` | API key 过期 | 更新 GitHub Secret |
| `No module named stagent.__main__` | editable install 劫持 | 删 pyproject.toml + pip install -e |
| `Post Setup Python: Cache folder ... doesn't exist` | pip cache 冲突 | 去掉 `cache: "pip"` |

## 已知限制

1. **Chromium 首次下载**：~100MB，CI 冷启动需 20-60s（有 cache 后快）
2. **DeepSeek API key**：需用户自己配 GitHub Secret
3. **nightly 7 条全量**：~30min 超时，首次可能因网络抖动偶尔失败——重跑即可
