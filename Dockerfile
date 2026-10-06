ARG PYTHON_IMAGE=python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 archivability \
    && useradd --system --uid 10001 --gid archivability \
       --home-dir /nonexistent --shell /usr/sbin/nologin archivability

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY methodology ./methodology

RUN python -m pip install '.[server]' \
    && python -m compileall -q /app/src

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "--factory", "archivability.asgi:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
