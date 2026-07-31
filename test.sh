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
curl -fsS "${BASE_URL}/health" | jq # >/dev/null
echo "Bridge is healthy. ----------------------------------------------------------------------------"

# echo "Listing models exposed by the bridge:"
# curl -fsS "${BASE_URL}/v1/models" | jq '.data[].id'

echo "Checking whether ${MODEL} is available in Ollama..."
if curl -fsS "${OLLAMA_URL}/api/tags" | jq -e --arg model "$MODEL" '.models[] | select(.name == $model)' >/dev/null; then
  echo "Model ${MODEL} is available in Ollama."
else
  echo "Model ${MODEL} was not found in Ollama. Run: ollama pull ${MODEL}" >&2
  exit 1
fi

# randomDate=$(date -d "2025-06-01 + $(( RANDOM % ( $(date -d "2026-06-30" +%s) - $(date -d "2025-06-01" +%s) + 86400 ) / 86400 )) days" "+%Y-%m-%d")
randomDate=$(date -d "2026-01-01 + $(( RANDOM % 150 )) days" "+%B %d, %Y")
echo "Randomly selected date: $randomDate"
randomCountry=$(shuf -n 1 -e "Argentina" "Australia" "Brazil" "Canada" "Denmark" "Egypt" "France" "Germany" "India" "Japan" "Kenya" "Mexico" "Norway" "Peru" "South Korea" "Spain" "Thailand" "United Kingdom" "Vietnam")
echo "Randomly selected country: $randomCountry"

instructionsRole="Role: You are a helpful assistant that provides accurate and concise information. When you provide information, ensure it is well-reasoned, factually correct, and supported by evidence. If you are unsure about an answer, clearly state your uncertainty and provide the best possible response based on available information. Avoid making assumptions or fabricating information. Always prioritize accuracy and clarity in your responses. You should use the available tools to gather information, verify facts, and provide comprehensive answers. If a question requires external information, use the appropriate tool to retrieve it before responding. Ensure that your responses are clear, structured, and easy to understand. Your response must be in a well-set Markdown format, with proper headings, bullet points, and code blocks where applicable. Avoid using overly technical jargon unless necessary, and provide explanations for any complex concepts. Your goal is to assist users effectively by providing reliable information and guidance."
instructionsTools="## MCP Tools Instructions

You have access to the following tools. Use them proactively when they improve accuracy, completeness, or reasoning quality. Prefer tools over guessing when information may be outdated, incomplete, or external.

### Available Tools

| Tool | Purpose | When to Use |
|------|---------|-------------|
| **sequential-thinking** | Structured multi-step reasoning | Complex problems, planning, debugging, multi-hop logic, or when you need to break a task into clear intermediate steps before answering. |
| **duckduckgo-search** | Privacy-focused web search | General factual queries, quick lookups, or when you want results less personalized than Google. Good default for most searches. |
| **google-search** | Broad web search (includes news, weather, local, etc.) | Current events, news, weather, real-time data, product info, or when richer/more comprehensive results are needed. Prefer over DuckDuckGo when freshness or coverage matters. |
| **fetch** | Retrieve full content from a specific URL | After a search returns a promising link, or when the user provides a URL. Use to extract accurate details, code, documentation, or full articles instead of relying on snippets. |
| **context7** | Up-to-date documentation for programming languages & frameworks | Any coding, API, library, or framework question where official or current docs are required. Prefer this over general search for technical accuracy. |
| **memory** | Persistent long-term memory | - Retrieve previously stored context or facts relevant to the current conversation.<br>- Save important information, user preferences, decisions, or key facts for future reference. |

### Usage Guidelines

1. **Decide early** whether a tool is needed. If the answer requires external, current, or precise information, call the tool(s) before generating the final response.
2. **Combine tools intelligently**:
   - Search → fetch promising URLs → reason with sequential-thinking if needed.
   - Use context7 for docs, then sequential-thinking to apply the information.
   - Check memory first for existing context before searching again.
3. **Be specific** in tool arguments (clear queries, exact URLs, precise memory keys/content).
4. **Prefer authoritative sources**: After searching, fetch official documentation, primary sources, or high-quality pages rather than relying solely on search snippets.
5. **Avoid unnecessary calls**: Do not search for information you already know confidently or that is static/common knowledge.
6. **Memory hygiene**: Only store durable, useful information. Retrieve only when relevant.

Use tools transparently and efficiently to deliver accurate, well-reasoned, up-to-date answers."

content="Use the 'fetch' MCP tool to retrieve the title of https://shamela.org and respond with only the title."
content="Use the 'context7' MCP tool to retrieve the documentation of the latest version of Laravel, as to what has changed from the previous version."
content="Use the 'duckduckgo-search' MCP tool to search for the latest news about AI from $randomCountry specifically in $randomDate and summarize the top 3 articles."
content="Generate a report regarding the issue between Abu Iyaad of Salafi Publications and Shaikh Arafat al-Muhammadi."
content="Generate a report regarding the accusations on the Rulers of UAE made by Bilal as-Salimee, Fawwaz al-Madkhali and Ali al-Hudhayfi al-Yemeni, search both English and Arabic sources, mention the URL links of sources to cross-verify."
content="Objective: Get online references and sources regarding the issue between Abu Iyaad of Salafi Publications and Shaikh Arafat al-Muhammadi, and the accusations on the Rulers of UAE made by Bilal as-Saalimee, Fawwaz al-Madkhali and Ali al-Hudhayfi al-Yemeni. Search both English and Arabic sources, mention the URL links of sources to cross-verify. Provide a summary of the findings."

content="*Topic*: Is calling someone a 'Zionist' considered making Takfir? If someone calls a Muslim Ruler as a 'Zionist', does that mean they are making Takfir on the ruler? Is this person considered a Takfiri or Khariji?"
objective="*Objective*: Get online references and sources regarding a given Islamic Topic, search in Arabic and English sources, give precedence to Salafi stream, get verbatim original Arabic text along with English translations. Cite each source with URL links to cross-verify. Provide a summary of the findings."
content="$objective
$content"

systemContent="$instructionsRole
---
$instructionsTools"

echo "Sending request to ${BASE_URL}/v1/chat/completions using model ${MODEL} with content:
$content"

dataPost=$(jq -n --arg model "$MODEL" --arg content "$content" --arg systemContent "$systemContent" '{
  model: $model,
  stream: false,
  messages: [
    {
      "role": "system",
      "content": $systemContent
    },{
      role: "user",
      content: $content
    }
  ],
  "temperature": 0.1
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
