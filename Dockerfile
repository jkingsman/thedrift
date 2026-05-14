FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY station/ station/
COPY run.py .
COPY analytics.py .

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data && \
    adduser --disabled-password --no-create-home drift && \
    chown -R drift:drift /app

USER drift

ENV DRIFT_DB_PATH=/app/data/drift.db

EXPOSE 8080

CMD ["python", "run.py"]
