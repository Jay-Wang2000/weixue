# syntax=docker/dockerfile:1
ARG BRIDGE_BASE_IMAGE=python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
FROM ${BRIDGE_BASE_IMAGE}
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=120 --retries 5 -r requirements.txt && rm requirements.txt
COPY weixue_bridge weixue_bridge
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "weixue_bridge"]
