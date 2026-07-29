# Stage 1: 构建前端
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build && npm cache clean --force

# Stage 2: Python 后端 + 运行环境
FROM python:3.12-slim

# 系统依赖：Xvfb、x11vnc、noVNC 及浏览器底层 C 库（不再重复安装 apt 系统的 chromium）
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 虚拟显示 + VNC + noVNC 依赖
    xvfb x11vnc novnc websockify \
    # 浏览器运行底库
    curl ca-certificates fonts-liberation libnss3 libatk-bridge2.0-0 \
    libdrm2 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libxkbcommon0 \
    libasound2 libpango-1.0-0 libcairo2 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app

# 安装 Python 依赖（无 pip 缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 只安装 Playwright Chromium 浏览器（共享系统中已安装的 C 库）
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium

# 复制后端代码
ARG APP_VERSION=dev
COPY . .
# 注入版本号
RUN echo "__version__ = \"${APP_VERSION}\"" > core/version.py
# 清理开发遗留源码
RUN rm -rf .venv frontend tests

# 复制前端构建产物
COPY --from=frontend-builder /app/static ./static

# 启动脚本
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# APP_PASSWORD: 运行时通过 -e APP_PASSWORD=xxx 设置
ENV APP_PASSWORD=""

EXPOSE 8000 6080 8889

ENTRYPOINT ["/docker-entrypoint.sh"]
