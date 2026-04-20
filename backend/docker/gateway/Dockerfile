# Gateway: listens on location ports, starts worker containers on demand.
# Build context = ./backend (set by docker-compose at repo root).
FROM python:3.10-slim

RUN pip install --no-cache-dir docker redis PySocks

WORKDIR /app
# Context is backend/ when built via docker-compose
COPY gateway.py openvpn_proxy_runner.py backend_docker.py ovpn_filter.py provider_auth.py ./

ENV CONFIG_PATH=/config/openvpn-proxy-config.json
ENV USE_DOCKER=1

CMD ["python", "gateway.py", "--config", "/config/openvpn-proxy-config.json"]
