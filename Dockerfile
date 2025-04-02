FROM python:3.12-bullseye

# install uv to run stdio clients (uvx)
RUN pip install --no-cache-dir uv
# install mcp-atlassian
RUN pip install --no-cache-dir mcp-atlassian

# install npx to run stdio clients (npx)
RUN apt-get update && apt-get install -y --no-install-recommends curl
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
RUN apt-get install -y --no-install-recommends nodejs tar jq vim wget

RUN wget https://github.com/grafana/mcp-grafana/releases/download/v0.2.3/mcp-grafana_Linux_x86_64.tar.gz -O /tmp/mcp-grafana_Linux_x86_64.tar.gz && \
    cd /tmp && tar -xzvf mcp-grafana_Linux_x86_64.tar.gz && \
    chmod 755 mcp-grafana && \
    mv mcp-grafana /usr/bin
    
COPY pyproject.toml .

## FOR GHCR BUILD PIPELINE
COPY mcp_bridge/__init__.py mcp_bridge/__init__.py
COPY README.md README.md

RUN uv sync

COPY mcp_bridge mcp_bridge

EXPOSE 8000

WORKDIR /mcp_bridge
ENTRYPOINT ["uv", "run", "main.py"]
