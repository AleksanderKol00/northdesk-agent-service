# syntax=docker/dockerfile:1
# One image for both the API and the Temporal worker; docker-compose.yml picks the command.

FROM python:3.14.8-slim-trixie
COPY --from=astral/uv:0.12.23 /uv /usr/local/bin/uv

# Byte-compile for faster start-up, copy out of the cache mount, and use the image's Python, never a downloaded one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Dependencies first, in their own layer, so a source change doesn't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# Then the project itself, installed as a regular (non-editable) package with its seed data.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Installed as root, run as an unprivileged user that can read but not change the code.
RUN useradd --system --uid 1000 --create-home app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER app

EXPOSE 8000
# The app comes from [tool.fastapi] entrypoint in /app/pyproject.toml.
CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000"]
