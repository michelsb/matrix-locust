# syntax=docker/dockerfile:1

FROM python:3.14-slim AS builder

ARG POETRY_VERSION=2.4.1

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_CACHE_DIR=/tmp/poetry-cache

WORKDIR /app

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

# Copy dependency files first so this layer stays cached when only source code
# changes.
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root --no-ansi \
    && rm -rf "${POETRY_CACHE_DIR}"


FROM python:3.14-slim AS runtime

ARG UID=1000
ARG GID=1000

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

RUN groupadd --gid "${GID}" matrix-locust \
    && useradd --uid "${UID}" --gid "${GID}" --create-home matrix-locust

COPY --from=builder /app/.venv /app/.venv
COPY --chown=matrix-locust:matrix-locust . .

USER matrix-locust

# An argument-less invocation prints help instead of starting an accidental
# load test.
ENTRYPOINT ["python", "experiments/run_factorial.py"]
CMD ["--help"]
