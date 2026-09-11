# tallyagent in server mode: the HTTP API, the web UI and the MCP server.
#
#   docker build -t tallyagent .
#   docker run --rm -p 8787:8787 \
#     -v "$PWD/config:/app/config:ro" -v tallyagent-data:/data \
#     -e DEEPSEEK_API_KEY -e TALLYAGENT_MCP_TOKEN \
#     tallyagent serve --config config/config.toml
#
# There is no tray icon and no screen perception in a container: both are
# desktop features, and Tier 3 computer use is unavailable by construction here.
# Point [tally] host at the Windows machine running TallyPrime.

FROM python:3.12-slim AS build
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# lxml wheels cover the common platforms; keep the build image free of a
# toolchain unless the wheel is genuinely missing.
COPY pyproject.toml README.md ./
COPY packages ./packages
RUN pip install --no-cache-dir . ".[mcp]"

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
# Never run the accounts daemon as root.
RUN useradd --create-home --uid 10001 tallyagent
WORKDIR /app

COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/tallyagent /usr/local/bin/tallyagent
COPY config ./config
COPY scripts ./scripts

RUN mkdir -p /data && chown tallyagent:tallyagent /data
USER tallyagent
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/health').read()"

ENTRYPOINT ["tallyagent"]
CMD ["serve", "--config", "config/config.toml"]
