FROM python:3.12-slim AS base-deps
WORKDIR /app
COPY requirements.txt requirements-rag.txt ./
RUN pip install --no-cache-dir -r requirements.txt

FROM base-deps AS rag-deps
RUN pip install --no-cache-dir -r requirements-rag.txt

FROM base-deps AS runtime
COPY backend ./backend
COPY evals ./evals
COPY scripts ./scripts
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
CMD ["uvicorn", "backend.formal_app:create_formal_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM rag-deps AS rag-runtime
COPY backend ./backend
COPY evals ./evals
COPY scripts ./scripts
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
