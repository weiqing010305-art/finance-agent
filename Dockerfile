FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-rag.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY evals ./evals
COPY scripts ./scripts
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
CMD ["uvicorn", "backend.formal_app:create_formal_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
