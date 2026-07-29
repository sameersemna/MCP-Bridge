#!/bin/bash

PORT=11410
# PORT=8080

# qwen3.6:27b-q8_0 minimax-m3:cloud minimax-m3:cloud llama3.2:latest llama3.2:1b
MODEL='llama3.2:latest'
# MODEL='deepseek-v4-flash:cloud'

# Test script

# ports:
#   - "11410:8000"
#   - "11411:11401"
#   - "11413:11403"

# curl http://localhost:$PORT/v1/models | jq '.data[]'
# exit 0

# Use MCP Google Search Weather to answer: What is the weather forecast for this week? Please search for current conditions.

# curl -X POST http://localhost:$PORT/v1/chat/completions \
#   -H "Content-Type: application/json" \
#   -d '{
#     "model": "llama3.2:1b",
#     "messages": [
#       {
#         "role": "user",
#         "content": "More MCPs Added, refresh MCP Tools. List all the MCP Tools in details. What are the commands you have access to in a list."
#       }
#     ]
#   }' > response.json

dataPost=$(jq -n --arg model "$MODEL" '{
  model: $model,
  messages: [
    {
      "role": "user",
      "content": "Use MCP sequential-thinking, memory and noapi-google-search to answer: What is the weather forecast for this week for Paris, France? Please search for current conditions. Also list other tools available to you from MCP google-search. List all the tools available to you from MCP google-search in a list."
    }
  ]
}')
printf "Data to POST:\n%s\n" "$dataPost"

curl -X POST "http://localhost:$PORT/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "$dataPost" > response.json

cat response.json | jq --raw-output '.choices[0].message.content'




# docker compose build --no-cache
# docker compose up -d

# docker compose logs mcp-bridge




    # "playwright-mcp": {
    #   "command": "uvx",
    #   "args": [
    #     "playwright-mcp"
    #   ]
    # },
    # "context7": {
    #   "command": "uvx",
    #   "args": [
    #     "@upstash/context7-mcp"
    #   ]
    # },
    # "abacus": {
    #   "command": "uvx",
    #   "args": [
    #     "mcp-abacus"
    #   ]
    # },
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

# sk-or-v1-REDACTED
# export OPENROUTER_API_KEY='sk-or-v1-REDACTED'
# curl https://openrouter.ai/api/v1/chat/completions \
#   -H "Content-Type: application/json" \
#   -H "Authorization: Bearer sk-or-v1-REDACTED" \
#   -d '{
#   "model": "openai/gpt-4o",
#   "messages": [
#     {
#       "role": "user",
#       "content": "What is the meaning of life?"
#     }
#   ]
# }'