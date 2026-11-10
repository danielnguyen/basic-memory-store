FROM ghcr.io/openai/tunnel-client:v0.0.19 AS tunnel
FROM python:3.12-slim-bookworm

WORKDIR /app
COPY --from=tunnel /usr/bin/tunnel-client /usr/local/bin/tunnel-client
COPY integrations/chatgpt_mcp/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY integrations/chatgpt_mcp/server.py ./server.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_COMMAND="python /app/server.py" \
    BMS_API_URL="http://basic-memory-store:8000" \
    HOME=/tmp

USER 65532:65532
ENTRYPOINT ["/usr/local/bin/tunnel-client", "run"]
