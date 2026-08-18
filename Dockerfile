FROM python:3.12-slim AS base-deps
WORKDIR /app
COPY requirements.txt requirements-rag.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /var/cache/huggingface \
    && chown -R appuser:appuser /app /var/cache/huggingface

FROM base-deps AS rag-deps
RUN pip install --no-cache-dir -r requirements-rag.txt

FROM base-deps AS runtime
COPY --chown=appuser:appuser backend ./backend
COPY --chown=appuser:appuser evals ./evals
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini ./alembic.ini
USER appuser
CMD ["uvicorn", "backend.formal_app:create_formal_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM rag-deps AS rag-runtime
COPY --chown=appuser:appuser backend ./backend
COPY --chown=appuser:appuser evals ./evals
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini ./alembic.ini
USER appuser
