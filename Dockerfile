FROM openclaw/hermes-webui:latest

COPY kubectl /usr/local/bin/kubectl

RUN chmod +x /usr/local/bin/kubectl && \
    pip install fastapi uvicorn pydantic --no-cache-dir && \
    rm -rf /root/.cache/pip
