FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# tzdata is needed for TZ=Europe/London; without it the hourly boundary is UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the whole tree.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY README.md ./
RUN uv sync --frozen --no-dev

# The job only makes outbound HTTPS calls. It needs no root and no ports.
RUN useradd --create-home --uid 10001 organiser && chown -R organiser /app
USER organiser

# config.toml and secrets/service-account.json arrive as read-only mounts.
ENTRYPOINT ["drive-organiser"]

# Every run renames and moves files. There is no preview mode.
CMD ["loop"]
