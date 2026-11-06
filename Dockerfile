# One image for the API and the worker.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.3 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3
COPY coachcover ./coachcover
# replay mode answers from the recorded model answers and plays the recorded voice note
COPY tests/fixtures ./tests/fixtures

RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "--factory", "coachcover.api:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
