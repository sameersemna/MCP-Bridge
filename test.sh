#!/bin/bash
set -euo pipefail

clear

PORT="${PORT:-11410}"
MODEL="${MODEL:-deepseek-v4-flash:cloud}"
TIMEOUT="${TIMEOUT:-600}"
BASE_URL="http://localhost:${PORT}"
OLLAMA_URL="http://localhost:11434"

rm -f response.json response.md

echo "Checking MCP bridge health at ${BASE_URL}/health..."
curl -fsS "${BASE_URL}/health" >/dev/null

echo "Bridge is healthy."

# echo "Listing models exposed by the bridge:"
# curl -fsS "${BASE_URL}/v1/models" | jq '.data[].id'

echo "Checking whether ${MODEL} is available in Ollama..."
if curl -fsS "${OLLAMA_URL}/api/tags" | jq -e --arg model "$MODEL" '.models[] | select(.name == $model)' >/dev/null; then
  echo "Model ${MODEL} is available in Ollama."
else
  echo "Model ${MODEL} was not found in Ollama. Run: ollama pull ${MODEL}" >&2
  exit 1
fi

content="Use the fetch MCP tool to retrieve the title of https://shamela.org and respond with only the title."
content="Use the context7 MCP tool to retrieve the documentation of the latest version of Laravel, as to what has changed from the previous version."
content="Use the ddg-search MCP tool to search for the latest news about AI from Germany specifically in June 2026 and summarize the top 3 articles. Also use sequential-thinking to provide a brief analysis of the implications of these news articles on the AI industry."

echo "Sending request to ${BASE_URL}/v1/chat/completions using model ${MODEL} with content: $content"

dataPost=$(jq -n --arg model "$MODEL" --arg content "$content" '{
  model: $model,
  stream: false,
  messages: [
    {
      role: "user",
      content: $content
    }
  ]
}')

echo "Sending request to ${BASE_URL}/v1/chat/completions using model ${MODEL}..."
# curl --fail --silent --show-error --max-time "$TIMEOUT" --connect-timeout 5 \
curl -X POST "${BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "$dataPost" > response.json

echo ''
echo "-- Response received and saved to response.json ------------------------------------"
if ! jq -e '.choices[0].message.content != null and (.choices[0].message.content | type == "string") and (.choices[0].message.content | length > 0)' response.json >/dev/null; then
  echo "No usable completion content returned." >&2
  cat response.json >&2
  exit 1
fi

echo "Extracting content from response.json and saving to response.md..."
jq -r '.choices[0].message.content' response.json > response.md

cat response.md
