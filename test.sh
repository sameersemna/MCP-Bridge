#!/bin/bash
set -euo pipefail
# clear

# ANSI color codes for terminal output
C_RESET=$'\033[0m'
C_BOLD=$'\033[1m'
C_RED=$'\033[31m'
C_GREEN=$'\033[32m'
C_YELLOW=$'\033[33m'
C_BLUE=$'\033[34m'
C_MAGENTA=$'\033[35m'
C_CYAN=$'\033[36m'

PORT="${PORT:-11410}"
MODEL="${MODEL:-deepseek-v4-flash:cloud}"
TIMEOUT="${TIMEOUT:-600}"
BASE_URL="http://localhost:${PORT}"
OLLAMA_URL="http://localhost:11434"

# glm-5.2:cloud
# kimi-k3:cloud

rm -f response.json response.md

echo "${C_CYAN}Checking MCP bridge health at ${BASE_URL}/health...${C_RESET}"
curl -fsS "${BASE_URL}/health" | jq '.mcp_servers[] | select((.status == "online")) | .name' # >/dev/null
echo "${C_GREEN}Bridge is healthy. ---------------------------------${C_RESET}"

# echo "Listing models exposed by the bridge:"
# curl -fsS "${BASE_URL}/v1/models" | jq > models.json
# curl -fsS "${BASE_URL}/v1/models" | jq '.data[].id' > models.txt
# exit

echo "${C_CYAN}Checking whether ${MODEL} is available in Ollama...${C_RESET}"
# if model name has 'free' in it, then it is a free model and does not need to be pulled from Ollama
if [[ "$MODEL" == *"/"* ]]; then
  echo "${C_GREEN}Model ${MODEL} is being used.${C_RESET}"
elif [[ "$MODEL" == *"free"* ]]; then
  echo "${C_GREEN}Model ${MODEL} is being used.${C_RESET}"
elif curl -fsS "${OLLAMA_URL}/api/tags" | jq -e --arg model "$MODEL" '.models[] | select(.name == $model)' >/dev/null; then
  echo "${C_GREEN}Model ${MODEL} is available in Ollama.${C_RESET}"
else
  echo "${C_RED}Model ${MODEL} was not found in Ollama. Run: ollama pull ${MODEL}${C_RESET}" >&2
  exit 1
fi

# randomDate=$(date -d "2025-06-01 + $(( RANDOM % ( $(date -d "2026-06-30" +%s) - $(date -d "2025-06-01" +%s) + 86400 ) / 86400 )) days" "+%Y-%m-%d")
randomDate=$(date -d "2026-01-01 + $(( RANDOM % 150 )) days" "+%B %d, %Y")
echo "${C_CYAN}Randomly selected date:${C_RESET} ${C_BOLD}$randomDate${C_RESET}"
randomCountry=$(shuf -n 1 -e "Argentina" "Australia" "Brazil" "Canada" "Denmark" "Egypt" "France" "Germany" "India" "Japan" "Kenya" "Mexico" "Norway" "Peru" "South Korea" "Spain" "Thailand" "United Kingdom" "Vietnam")
echo "${C_CYAN}Randomly selected country:${C_RESET} ${C_BOLD}$randomCountry${C_RESET}"

FYI="# FYI
My Location: Berlin, Germany
My Timezone: Europe/Berlin
My Date: $(date -d "now" +'%B %d, %Y')
My Time: $(date -d "now" +'%H:%M:%S (%z)')
My preferred language: English
Other languages I can understand: Arabic, Urdu, Hindi, Marathi, German"
# echo "$FYI"
# echo '------------------------------'

# content="Use the 'fetch' MCP tool to retrieve the title of https://shamela.org and respond with only the title."
# content="Use the 'context7' MCP tool to retrieve the documentation of the latest version of Laravel, as to what has changed from the previous version."
# content="Use the 'duckduckgo-search' MCP tool to search for the latest news about AI from $randomCountry specifically in $randomDate and summarize the top 3 articles."
# content="Generate a report regarding the issue between Abu Iyaad of Salafi Publications and Shaikh Arafat al-Muhammadi."
# content="Generate a report regarding the accusations on the Rulers of UAE made by Bilal as-Salimee, Fawwaz al-Madkhali and Ali al-Hudhayfi al-Yemeni, search both English and Arabic sources, mention the URL links of sources to cross-verify."
# content="Objective: Get online references and sources regarding the issue between Abu Iyaad of Salafi Publications and Shaikh Arafat al-Muhammadi, and the accusations on the Rulers of UAE made by Bilal as-Saalimee, Fawwaz al-Madkhali and Ali al-Hudhayfi al-Yemeni. Search both English and Arabic sources, mention the URL links of sources to cross-verify. Provide a summary of the findings."

# content="*Topic*: Is calling someone a 'Zionist' considered making Takfir? If someone calls a Muslim Ruler as a 'Zionist', does that mean they are making Takfir on the ruler? Is this person considered a Takfiri or Khariji?"
# content="*Topic*: Is considering someone a supporter of 'Wahadatul Adyaan' considered making Takfir? If someone calls a Muslim Ruler as such, does that mean they are making Takfir on the ruler? Is this person considered a Takfiri or Khariji?"
# content="*Topic*: Shaikh Nizar ibn Hashim al-Sudani and Shaikh Bilal Abdul Ghani al-Saalimee have called the Rulers of UAE as 'Zionists' (in their Facebook posts) and have accused them of supporting the Jews and the Zionist agenda. This has been spread by them and their followers on Social Media like Facebook, Twitter and Telegram. Is this considered making Takfir on the Rulers of UAE? Is this person considered a Takfiri or Khariji? Search both English and Arabic sources, mention the URL links of sources to cross-verify. Provide a summary of the findings."
# content="*Topic*: Shaikh Bilal Abdul Ghani al-Saalimee have called the Rulers of UAE and Shaikh Muhammad Ghalib al-Umari as calling to 'Wahadatul Adyaan' (in his Facebook posts), is this considered making Takfir on the Rulers of UAE and Shaikh Muhammad Ghalib al-Umari? Is this person considered a Takfiri or Khariji? Search both English and Arabic sources, mention the URL links of sources to cross-verify. Provide a summary of the findings."
# content="*Topic*: Shaikh Fawwaz al-Madkhali has criticized the Rulers of UAE using Newspaper articles and reports of western media (in his Facebook posts) and have accused them of supporting the Jews and the Zionist agenda. This has been spread by him and his followers on Social Media like Facebook, Twitter and Telegram. Is this considered making Takfir on the Rulers of UAE? Is this person considered a Takfiri or Khariji? Is it from the Salafi principles to use reports from Newspapers and western media against Muslim rulers? Search both English and Arabic sources, mention the URL links of sources to cross-verify. Provide a summary of the findings."
# content="*Topic*: Shaikh Fawwaz al-Madkhali, Shaikh Nizar al-Sudani and Shaikh Bilal al-Salimihas criticized the Rulers of UAE, there is another person named Shaikh Ali al-Hudhayfi al-Yemeni in their camp, I want to search for his posts and statements regarding the Rulers of UAE, and see if he has also accused them of supporting the Jews and the Zionist agenda, and if he has also called them as 'Zionists' or supporters of 'Wahadatul Adyaan'. Is this considered making Takfir on the Rulers of UAE? Is this person considered a Takfiri or Khariji? Search both English and Arabic sources, mention the URL links of sources to cross-verify. Provide a summary of the findings."

systemContent=$(cat ./prompts/compressed/system.md)
content=$(cat ./prompts/compressed/content.md)
contentShort=$(cat ./prompts/compressed/content.md | head -c 300)
echo "${C_CYAN}Sending prompt to ${BASE_URL}/v1/chat/completions using model ${MODEL} with content:${C_RESET}
---
$contentShort ...
---"
systemContent="${systemContent}
---
${FYI}"
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

echo "${C_CYAN}CURL request to ${BASE_URL}/v1/chat/completions using model ${MODEL}...${C_RESET}"
# curl --fail --silent --show-error --max-time "$TIMEOUT" --connect-timeout 5 \
curl -X POST "${BASE_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "$dataPost" > response.json

echo ''
echo "${C_GREEN}-- Response received and saved to response.json from model ${MODEL} ------------------------------------${C_RESET}"
if ! jq -e '.choices[0].message.content != null and (.choices[0].message.content | type == "string") and (.choices[0].message.content | length > 0)' response.json >/dev/null; then
  echo "${C_RED}No usable completion content returned from model ${MODEL}.${C_RESET}" >&2
  cat response.json >&2
  exit 0
fi

echo "${C_CYAN}Extracting content from response.json and saving to response.md from model ${MODEL}...${C_RESET}"
jq -r '.choices[0].message.content' response.json > response.md

# Some reasoning models (e.g. Liquid LFM2.5) advertise `tools` support but do not
# emit structured OpenAI `tool_calls`. Instead they write pseudo tool-call markers
# as plain text inside `content` (e.g. `<|tool_call_start|>`, `<tool_call>`,
# `<function=...>`). The bridge cannot execute these, so skip such models.
if grep -qE '<\|tool_call|<tool_call|<function=|<\|function' response.md; then
  echo "${C_YELLOW}WARNING: Model ${MODEL} emitted pseudo tool-call markers as plain text${C_RESET}" >&2
  echo "${C_YELLOW}         (e.g. <|tool_call_start|> / <tool_call> / <function=...>).${C_RESET}" >&2
  echo "${C_YELLOW}         This model does not support structured tool calls via the bridge.${C_RESET}" >&2
  echo "${C_YELLOW}         Skipping model ${MODEL}.${C_RESET}" >&2
  rm -f response.md
  exit 0
fi

cat response.md | head -c 250

# bolt://localhost:7687
# curl http://localhost:7474 neo4j password123
