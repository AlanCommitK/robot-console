FROM python:3.12-slim

# 可选的 PyPI 镜像源（国内网络可用 https://pypi.tuna.tsinghua.edu.cn/simple），由 docker compose 从 .env 传入；
# 默认为空 = 官方 PyPI。
ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先只复制依赖清单再安装：源码改动不会让这一层缓存失效，重新构建时不用重新下载依赖。
COPY requirements.txt .
# ${VAR:+...} 是 shell 的条件展开：PIP_INDEX_URL 非空时展开成 --index-url "<地址>"，为空时整段消失，
# 和没有这个参数完全一样。（ARG 在 RUN 里也是同名环境变量，pip 会忽略值为空的 PIP_INDEX_URL。）
RUN pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements.txt

# 用非 root 用户运行：即使应用被攻破，攻击者也拿不到容器内的 root 权限。
RUN useradd --create-home --uid 10001 app
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
USER app

EXPOSE 8000

# urlopen 遇到非 2xx（例如依赖故障时的 503）会抛异常，python 以非 0 退出 => 容器被标记为 unhealthy。
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"]

# --proxy-headers / --forwarded-allow-ips：信任 Nginx 传来的 X-Forwarded-For/Proto，
# 让日志和 request.client 里看到的是真实客户端地址而不是 Nginx 的地址。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
