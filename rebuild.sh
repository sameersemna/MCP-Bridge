#!/bin/bash
set -euo pipefail

clear

echo "Rebuilding the MCP bridge and starting it up..."
# docker compose build --no-cache
docker compose up -d --build --force-recreate
sleep 2

echo ''
echo "Building the MCP bridge completed. Starting..."
docker compose up -d
sleep 2

echo ''
echo "Checking MCP bridge health at http://localhost:11410/health..."
curl "http://localhost:11410/health" | jq
# if curl -fsS "http://localhost:11410/health" >/dev/null; then
#   echo "Bridge is healthy."
# else
#   echo "Bridge is not healthy. Check the logs for details." >&2
#   docker compose logs -f mcp-bridge
#   exit 1
# fi

echo ''
echo "MCP bridge rebuild and startup completed successfully."
# docker compose logs mcp-bridge
docker compose logs -f mcp-bridge
