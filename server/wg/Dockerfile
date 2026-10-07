# syntax=docker/dockerfile:1
ARG WG_BASE_IMAGE=alpine:3.23.4@sha256:5b10f432ef3da1b8d4c7eb6c487f2f5a8f096bc91145e68878dd4a5019afde11
FROM ${WG_BASE_IMAGE}
RUN apk add --no-cache wireguard-tools=1.0.20250521-r1 iptables=1.8.11-r1
COPY entrypoint.sh healthcheck.sh /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
