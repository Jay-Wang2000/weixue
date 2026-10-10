# syntax=docker/dockerfile:1
ARG AGENT_BASE_IMAGE=python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
FROM ${AGENT_BASE_IMAGE}
RUN useradd --uid 1000 --create-home --home-dir /home/agent --shell /usr/sbin/nologin agent
WORKDIR /app
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --default-timeout=120 --retries 5 --no-deps -r /tmp/requirements.lock \
    && pip check && rm /tmp/requirements.lock
COPY --from=hamcp . weixue_hamcp
COPY weixue_agent weixue_agent
COPY tools tools
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/home/agent
USER agent
EXPOSE 8010
CMD ["python", "-m", "weixue_agent"]
