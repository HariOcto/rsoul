FROM python:3.11

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN sed -i 's/\r$//' run.sh

RUN chmod +x run.sh

ENV PYTHONUNBUFFERED=1
ENV IN_DOCKER=Yes

# Unhealthy for stale liveness or a durable-state write failure (see rsoul/health.py).
# Nothing is restarted automatically by this health check.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "rsoul.health"]

ENTRYPOINT ["bash", "run.sh"]
