# Test script

# curl http://localhost:11410/v1/models | jq '.data[]'
# exit 0

# qwen3.6:27b-q8_0 minimax-m3:cloud minimax-m3:cloud llama3.2:latest llama3.2:1b
# Use MCP Google Search Weather to answer: What is the weather forecast for this week? Please search for current conditions.

curl -X POST http://localhost:11410/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.2:1b",
    "messages": [
      {
        "role": "user",
        "content": "More MCPs Added, refresh MCP Tools. List all the MCP Tools in details. What are the commands you have access to in a list."
      }
    ]
  }' > response.json

cat response.json | jq --raw-output '.choices[0].message.content'




# docker compose build --no-cache
# docker compose up -d

# docker compose logs mcp-bridge




    # "filesystem": {
    #   "command": "npx",
    #   "args": [
    #     "-y",
    #     "@modelcontextprotocol/server-filesystem",
    #     "/home/sameer/projects"
    #   ]
    # },
    # "google-search": {
		# 	"type": "http",
		# 	"url": "http://latitude:11403/mcp",
		# 	"auth": {
		# 		"type": "none"
		# 	},
		# 	"requestTimeout": 10000
    # },
    # "google-stitch-proxy": {
    #   "command": "npx",
    #   "args": [
    #     "-y", 
    #     "@_davideast/stitch-mcp", 
    #     "serve", 
    #     "--port", "11401"
    #   ],
    #   "env": {
    #     "STITCH_API_KEY": "API_KEY"
    #   }
    # }