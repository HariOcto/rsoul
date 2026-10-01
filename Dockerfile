FROM python:3.11

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN sed -i 's/\r$//' run.sh

RUN chmod +x run.sh

ENV PYTHONUNBUFFERED=1
ENV IN_DOCKER=Yes

# Unhealthy only when R:soul's status file hasn't been updated for longer than a run plus
# the wait between runs (see rsoul/health.py); nothing is restarted automatically
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "rsoul.health"]

ENTRYPOINT ["bash", "run.sh"]