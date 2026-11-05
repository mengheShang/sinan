# API、Worker、Executor 共用的 Python 镜像，通过不同的启动命令区分角色
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# 先只复制依赖清单，代码改动时可以复用依赖层的缓存
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY apps/__init__.py ./apps/__init__.py
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY apps/executor ./apps/executor

RUN useradd --create-home --uid 10001 sinan
USER sinan

ENV PATH="/srv/.venv/bin:$PATH"
