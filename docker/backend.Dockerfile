# Shared Python image for API, Worker and Executor; the start command picks the role
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Copy the dependency manifests first so code changes reuse the cached dependency layer
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY apps/__init__.py ./apps/__init__.py
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY apps/executor ./apps/executor
# The evaluation runner, so a run in a Worker container uses the image's code
COPY evals ./evals

RUN useradd --create-home --uid 10001 sinan
USER sinan

ENV PATH="/srv/.venv/bin:$PATH"

# The commit the image was built from, for evaluation report headers (Issue #87).
# Last, so a new value rebuilds only this layer. Set by compose from SINAN_COMMIT
ARG SINAN_COMMIT=unknown
ENV SINAN_COMMIT=$SINAN_COMMIT
