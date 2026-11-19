# CI 配置说明

## 前置条件（一次性）

### 1. 仓库创建（如未创建）

```bash
git remote add origin https://github.com/<you>/TestAgent.git
git push -u origin main
```

### 2. 配置 DeepSeek API Key Secret

GitHub → Your Repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value |
|---|---|
| `DEEPSEEK_API_KEY` | `sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`（你的 DeepSeek API key）

没有 API key？注册 https://platform.deepseek.com/ 免费领。

### 3. 配置 YAML 校验（可选）

vscode 里装 `vscode-yaml`（Red Hat），让 workflow yml 有 schema 校验 + 提示。

## 触发规则

| 事件 | 触发 job | 说明 |
|---|---|---|
| **push 到 main** | `smoke` | ~5min 跑完 |
| **创建 / 更新 PR** | `smoke` | PR 门禁，exit 0/2 可合，exit 1 阻断 |
| **每天 UTC 22:00（北京 06:00）** | `nightly` | 7 条全量回归 |

## exit code 三态

`scripts/ci_run.py` 翻译 stagent CLI 的 exit code：

| CLI exit | CI 显示 | 语义 |
|---|---|---|
| 0 | ✅ 绿 | 全 passed |
| 2 | ✅ 绿 | 有 expected_fail（known bug），无 unexpected_fail |
| 1 | ❌ 红 | 有 unexpected_fail 或 error → 阻断 |

## Artifacts

每次 CI 跑完自动上传：

| Job | Artifact 名 | 保留 | 内容 |
|---|---|---|---|
| smoke | `smoke-{event}-{sha}` | 7 天 | 报告 .md / .html / .json + 截图 |
| nightly | `nightly-{run_id}` | 30 天 | 同上 |

下载方式：CI 页面 → **Artifacts** → 下载 zip → 本地解压打开 `report.html`。

## 本地模拟 CI（Docker）

```bash
# 构建镜像（约 1.5GB，首次 ~5min）
docker build -f Dockerfile -t testagent-ci .

# 跑 selftest（离线自检，不启动浏览器）
docker run --rm testagent-ci python scripts/selftest.py

# 跑 smoke_login.yaml（需要 DeepSeek API key）
docker run --rm -e DEEPSEEK_API_KEY=sk-xxx testagent-ci \
    python scripts/ci_run.py suites/smoke_login.yaml
```

## 排错

### CI 报 "APIConnectionError: Connection refused"

Ollama 只在 localhost 上跑，CI runner 访问不到。检查 `config.ci.example.yaml` 里 provider 是否为 `deepseek`，base_url 是否为 `https://api.deepseek.com/v1`。

### CI 报 "npx @playwright/mcp 启动失败"

Playwright 官方 action 已处理 system deps。如果还是不行：
1. 检查 `config.ci.example.yaml` 里 browser.headless 是否为 true
2. 检查 runner logs 里有没有 `PlaywrightError: Executable doesn't exist` → 重新跑 workflow（Chromium 可能安装超时）

### CI 跑了 45min 被 timeout

`max_steps: 40` + 7 条 × ~5min ≈ 35min。如果 nightly 经常超时，把 timeout-minutes 改到 60。

### exit code 是 2（known bug 绿）但想看哪条 expected_fail 了

CI logs 里找 `⚠️ 预期失败` 标记，或者下载 artifacts 打开 `report.md` 看三标记。
