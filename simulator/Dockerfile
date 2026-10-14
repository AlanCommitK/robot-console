FROM python:3.12-slim

# 可选的 PyPI 镜像源（国内网络可用 https://pypi.tuna.tsinghua.edu.cn/simple），由 docker compose 从 .env 传入；
# 默认为空 = 官方 PyPI。
ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# 先只拷贝依赖清单再安装：依赖不变时这一层直接命中构建缓存，只改代码不会重装依赖。
COPY requirements.txt .
# PIP_INDEX_URL 非空时才展开成 --index-url "<地址>"，为空时整段消失（写法同 backend/Dockerfile）。
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements.txt

COPY robot_model.py sim.py ./

# 不用 root 跑业务进程：即使代码有漏洞，进程在容器里也只有普通用户权限。
RUN useradd --system --uid 10001 --no-create-home sim
USER sim

# exec 形式（JSON 数组）让 python 直接成为 PID 1，`docker stop` 发出的 SIGTERM 能直达
# sim.py 注册的信号处理器；shell 形式 `CMD python sim.py` 会多套一层 /bin/sh，信号被 sh 吞掉。
CMD ["python", "sim.py"]
