# =====================================================================
# TestAgent stagent CI 基础镜像
# 基于 Playwright 官方 Python 镜像（已包含 Chromium + system deps + xvfb + Node）
# 本地构建：docker build -f Dockerfile -t testagent-ci .
# CI 使用：GitHub Actions 直接在这个镜像里跑 python -m stagent
# =====================================================================

# Playwright 官方 Python 镜像
#   - 自带 Chromium + 完整 system deps（libgtk / xvfb / fonts 等）
#   - 自带 Python 3.12 + pip
#   - 自带 Node.js 20.x（@playwright/mcp 是 Node 进程）
#   - 约 1.5GB
FROM mcr.microsoft.com/playwright/python:latest

# 避免交互式提示
ENV DEBIAN_FRONTEND=noninteractive \
    # xvfb 虚拟显示服务器（headless Chromium 需要）
    DISPLAY=:99 \
    # 让 Python 输出立即刷新（CI 日志实时可见）
    PYTHONUNBUFFERED=1 \
    # Node 相关
    NODE_OPTIONS=--max-old-space-size=2048 \
    # stagent 配置
    STAGENT_BROWSER_HEADLESS=true

# 工作目录
WORKDIR /app

# 先拷 requirements 单独装（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷 stagent 包和 suites
COPY stagent/ ./stagent/
COPY scripts/ ./scripts/
COPY suites/ ./suites/
COPY config.example.yaml .

# 预装 @playwright/mcp 的 node_modules（npx 会缓存）
# 用 latest 版本，@playwright/mcp@latest 在 config.yaml 里指定
RUN npm install -g @playwright/mcp@latest 2>&1 || \
    echo "npm install -g 失败，npx 会在首次运行时自动下载"

# xvfb 启动脚本（可选，Playwright headless 模式可以不用）
# 留一个 entrypoint 方便手动调试
RUN echo '#!/bin/bash\nservice xvfb start || true\nexec "$@"' > /entrypoint.sh && \
    chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

# 默认命令：跑 selftest（离线自检，不启动浏览器）
CMD ["python", "scripts/selftest.py"]
