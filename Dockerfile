FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    RSS_WATCH_CONFIG=/app/container.toml
RUN groupadd --gid 10001 rss-watch && useradd --uid 10001 --gid rss-watch \
    --no-create-home --shell /usr/sbin/nologin rss-watch \
    && mkdir -p /app /data /credentials \
    && chown rss-watch:rss-watch /data /credentials \
    && chmod 0700 /data /credentials
COPY --from=build /opt/venv /opt/venv
COPY deploy/container.toml /app/container.toml
WORKDIR /app
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["rss-watch-container"]
CMD ["web"]
