# syntax=docker/dockerfile:1.7
# One image, several roles (api | agent | approver), selected by the command. NOT BUILT on the authoring
# machine (no Docker there) - see README "Status of the deliverables".

FROM python:3.11-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never
WORKDIR /src
# Dependencies first (cached layer), then the project itself, both pinned by uv.lock.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-install-project
COPY src ./src
RUN uv sync --frozen --no-editable

FROM python:3.11-slim AS runtime
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app:app /data
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    OPS_AGENT_DATA_DIR=/data \
    OPS_AGENT_PROVIDER=offline
USER app
WORKDIR /home/app
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["ops-agent"]
CMD ["serve-api", "--host", "0.0.0.0", "--port", "8765"]
