FROM python:3.15-slim

# uv, which installs the dependencies from uv.lock: from its own image, at the
# version the workflow uses too (.github/workflows/tests.yml).
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv

# UID 1000 is usually your user on the host, so it can read your config.toml.
RUN useradd --uid 1000 nanosaas

WORKDIR /app
# The dependencies first, in a layer of their own, so a change to the code doesn't
# install them again: exactly the versions of uv.lock, without the tests', with the
# image's Python, and compiled already, since the app's user can't write to them.
COPY pyproject.toml uv.lock ./
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
RUN uv sync --locked --no-dev --no-cache
ENV PATH="/app/.venv/bin:$PATH"
COPY . .

# Sends print() output to docker compose logs right away.
ENV PYTHONUNBUFFERED=1
USER nanosaas
EXPOSE 8000
CMD ["uvicorn", "main_api:app", "--host", "0.0.0.0", "--port", "8000"]
