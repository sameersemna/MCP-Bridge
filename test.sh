#!/bin/bash
set -euo pipefail

clear

PORT="${PORT:-11410}"
MODEL="${MODEL:-deepseek-v4-flash:cloud}"
TIMEOUT="${TIMEOUT:-600}"
BASE_URL="http://localhost:${PORT}"
OLLAMA_URL="http://localhost:11434"

# glm-5.2:cloud
# kimi-k3:cloud

rm -f response.json response.md

echo "Checking MCP bridge health at ${BASE_URL}/health..."
curl -fsS "${BASE_URL}/health" | jq # >/dev/null
echo "Bridge is healthy. ---------------------------------"

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

FYI="# FYI
My Location: Berlin, Germany
My Timezone: Europe/Berlin
My Date: $(date -d "now" +'%B %d, %Y')
My Time: $(date -d "now" +'%H:%M:%S (%z)')
My preferred language: English
Other languages I can understand: Arabic, Urdu, Hindi, Marathi, German"
echo "$FYI"
echo '------------------------------'

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

content=$(cat ./prompts/content.md)
contentShort=$(cat ./prompts/content.md | head -c 250)
echo "Sending request to ${BASE_URL}/v1/chat/completions using model ${MODEL} with content:
---
$contentShort ...
---"
objective=$(cat ./prompts/content.md)
content="${FYI}
---
${objective}

${content}
"

systemContent=$(cat ./prompts/system.md)
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

cat response.md | head -c 250
