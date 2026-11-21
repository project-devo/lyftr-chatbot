# ---- build: install deps into a venv ----
FROM python:3.12-slim AS build
WORKDIR /app
RUN python -m venv /venv
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

# ---- test: `make test` (docker build --target test) ----
FROM build AS test
ENV PATH=/venv/bin:$PATH LITELLM_LOCAL_MODEL_COST_MAP=True
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY . .
CMD ["python", "-m", "pytest", "-q"]

# ---- runtime: slim image, non-root, SQLite + LiteLLM cache on /data ----
FROM python:3.12-slim AS runtime
ENV PATH=/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    DATABASE_URL=sqlite:////data/app.db \
    CACHE_DIR=/data/litellm-cache
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app:app /data
COPY --from=build /venv /venv
WORKDIR /app
COPY app ./app
USER app
EXPOSE 8000
# Render injects $PORT; locally it defaults to 8000.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
