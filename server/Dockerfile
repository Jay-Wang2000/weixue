# syntax=docker/dockerfile:1
ARG SERVER_BASE_IMAGE=ghcr.io/xinnan-tech/xiaozhi-esp32-server:server-base@sha256:dc44d9acdec135d7c2100cfd67fb352c46824eed6cc53dcd8aff1bc9c0832c4e
FROM ${SERVER_BASE_IMAGE}
ARG UPSTREAM_COMMIT=unset
LABEL org.opencontainers.image.revision=${UPSTREAM_COMMIT}
COPY requirements-overlay.lock /tmp/requirements-overlay.lock
RUN pip install --no-cache-dir --no-deps --default-timeout=120 --retries 5 -r /tmp/requirements-overlay.lock \
    && pip check \
    && rm /tmp/requirements-overlay.lock
COPY --from=upstream --exclude=**/__pycache__ main/xiaozhi-server .
COPY --from=overlay --exclude=**/__pycache__ . .
CMD ["python", "weixue_main.py"]
