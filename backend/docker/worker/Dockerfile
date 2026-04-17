# Worker: one OpenVPN + pproxy per container. .ovpn mounted at run.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openvpn \
    python3 \
    python3-pip \
    ca-certificates \
    && pip3 install --break-system-packages --no-cache-dir pproxy \
    && rm -rf /var/lib/apt/lists/*

COPY docker/worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Defaults only; gateway sets real values at container start via backend_docker.start_docker_backend()
ENV OVPN_FILE=""
ENV AUTH_USER=""
ENV AUTH_PASS=""
ENV PROXY_USER=""
ENV PROXY_PASS=""

VOLUME /ovpn

ENTRYPOINT ["/entrypoint.sh"]
