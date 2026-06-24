# Test script

curl http://localhost:11410/v1/models
# exit 0

curl -X POST http://localhost:11410/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6:27b-q8_0",
    "messages": [
      {
        "role": "user",
        "content": "Use MCP Google Search Weather to answer: What is the weather forecast for this week? Please search for current conditions."
      }
    ]
  }'




# docker compose build --no-cache
# docker compose up -d

# docker compose logs mcp-bridge
