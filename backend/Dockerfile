FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY run.py ./
RUN pip install --no-cache-dir .

RUN useradd --system --uid 10001 --create-home reconciliation \
    && mkdir -p /data \
    && chown -R reconciliation:reconciliation /app /data

USER reconciliation
EXPOSE 8000
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=2", "--timeout=180", "--access-logfile=-", "run:app"]
