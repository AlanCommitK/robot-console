# =============================================================================
# 多阶段构建（multi-stage build）：
#   第一阶段用 Node 把 React + TypeScript 源码打包成静态文件；
#   第二阶段只把打包结果复制进 Nginx 镜像。最终镜像里没有 Node、node_modules 和源码，体积小、攻击面也小。
# 两个基础镜像都是官方的多架构镜像，Apple Silicon（arm64）和 x86 都能直接用。
# =============================================================================

# ---------- 第一阶段：构建 ----------
FROM node:22-alpine AS build

# 可选的 npm 镜像源（国内网络可用 https://registry.npmmirror.com），由 docker compose 从 .env 传入；
# 默认为空 = 官方 registry.npmjs.org。
# package-lock.json 里记录的下载地址都是 https://registry.npmjs.org/...，npm 的 replace-registry-host
# 默认值（npmjs）会把这个主机名换成 --registry 指定的源，所以用镜像源时 lock 文件不用改。
ARG NPM_REGISTRY=""

WORKDIR /app

# 先只复制依赖清单并安装依赖：只要 package.json / package-lock.json 没变，这一层就能命中 Docker 构建缓存，
# 之后只改业务代码时，重新构建不用再下载一遍依赖。
# npm ci（而不是 npm install）严格按 lock 文件安装，保证每次构建装到的版本完全一致。
COPY package.json package-lock.json ./
# NPM_REGISTRY 非空时才展开成 --registry="<地址>"，为空时整段消失（写法同 backend/Dockerfile）。
RUN npm ci ${NPM_REGISTRY:+--registry="$NPM_REGISTRY"} --no-audit --no-fund

# 再复制源码并构建：npm run build = 类型检查（tsc）+ 打包（vite build），产物输出到 /app/dist
COPY . .
RUN npm run build

# ---------- 第二阶段：运行 ----------
FROM nginx:1.27-alpine

# 用项目里的完整 nginx.conf 替换默认配置；默认的 conf.d/default.conf 不再被引用，删掉以免读者误以为它生效。
# 同时清空镜像自带的欢迎页（index.html、50x.html），站点目录里只留我们自己的构建产物
RUN rm -rf /etc/nginx/conf.d/default.conf /usr/share/nginx/html/*
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# 容器健康检查：请求 Nginx 自己的 /healthz（不依赖后端）。用 127.0.0.1 而不是 localhost，
# 因为 Alpine 里 localhost 可能先解析成 IPv6 的 ::1，而这里只监听了 IPv4
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1

# 启动命令沿用官方镜像的默认值：nginx -g 'daemon off;'（前台运行，容器主进程不能退出）
