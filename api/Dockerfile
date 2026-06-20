FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app/api

# System deps (kept minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt /app/api/requirements.txt
RUN pip install --no-cache-dir -r /app/api/requirements.txt

COPY api /app/api
COPY db /app/db

RUN test -f /app/db/baseline.sql \
 && test -d /app/db/migrations/managed \
 && test -f /app/api/tools/schema_migrations.py

EXPOSE 8000
CMD ["sh", "-lc", "python -m tools.schema_migrations check && uvicorn main:app --host 0.0.0.0 --port 8000"]
