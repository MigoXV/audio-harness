FROM registry.cn-hangzhou.aliyuncs.com/migo-dl/node:24-alpine-pnpm-builder AS frontend-build

WORKDIR /app/src/web

COPY src/web/package.json src/web/pnpm-lock.yaml src/web/pnpm-workspace.yaml ./
RUN pnpm config set registry https://registry.npmmirror.com && \
    pnpm install --frozen-lockfile

COPY src/web/ ./
RUN pnpm build

# 基于已经包含 Poetry 的基础镜像
FROM registry.cn-hangzhou.aliyuncs.com/migo-dl/python:3.10.18-poetry-0-4-1 AS runtime

# 设置工作目录
WORKDIR /app

# 安装音频处理所需的系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# 拷贝依赖清单，并用稳定的占位 README 满足项目元数据校验
COPY pyproject.toml poetry.lock ./
RUN touch README.md && \
    mkdir -p src/audio_harness && \
    touch src/audio_harness/__init__.py && \
    poetry install --no-root

# 拷贝源代码和真实 README；文档变化不会使依赖安装层失效
COPY . .

# 拷贝前端构建产物，运行时不再依赖 Node.js 或 Nginx
COPY --from=frontend-build /app/src/web/dist /app/src/web/dist

# 安装当前包
RUN poetry install

# 暴露 gRPC 与 HTTP/HTTPS/WSS 服务端口
EXPOSE 50051 8000

# 默认入口
CMD ["poetry", "run", "audio-harness"]
