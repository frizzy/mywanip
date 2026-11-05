# Serves the API and UI on :8787. State (SQLite) lives in the /data volume.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/frizzy/mywanip" \
      org.opencontainers.image.description="Dynamic WAN IP resolver: updates DNS, serves the IP over REST/SSE, fires webhooks" \
      org.opencontainers.image.licenses="MIT"
RUN useradd --system --uid 10001 --no-create-home mywanip \
    && mkdir /data && chown mywanip /data
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 MYWANIP_DATA=/data MYWANIP_HOST=0.0.0.0
USER mywanip
VOLUME /data
EXPOSE 8787
ENTRYPOINT ["mywanip"]
