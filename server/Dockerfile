ARG SERVER_BASE_IMAGE=ghcr.io/xinnan-tech/xiaozhi-esp32-server:server-base@sha256:dc44d9acdec135d7c2100cfd67fb352c46824eed6cc53dcd8aff1bc9c0832c4e
FROM ${SERVER_BASE_IMAGE}
ARG UPSTREAM_COMMIT=unset
LABEL org.opencontainers.image.revision=${UPSTREAM_COMMIT}
COPY --from=upstream main/xiaozhi-server .
COPY --from=overlay . .
CMD ["python", "app.py"]
