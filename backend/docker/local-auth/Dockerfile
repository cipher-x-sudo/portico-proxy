# Gateway image for the local auth-routing deployment.
# Build context = ./backend (set by docker-compose.local-auth.yml at repo root).
FROM python:3.10-slim

RUN pip install --no-cache-dir docker redis PySocks psycopg2-binary

WORKDIR /app
COPY gateway.py openvpn_proxy_runner.py backend_docker.py ovpn_filter.py provider_auth.py upstream_proxy.py storage.py sd_farm.py ./

ENV CONFIG_PATH=/config/openvpn-proxy-config.json
ENV USE_DOCKER=1
ENV AUTH_ROUTING_ENABLED=1
ENV AUTH_HTTP_PORT=58680
ENV AUTH_SOCKS_PORT=58681

CMD ["python", "gateway.py", "--config", "/config/openvpn-proxy-config.json"]
