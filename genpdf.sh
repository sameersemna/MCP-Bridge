#!/usr/bin/env bash
set -euo pipefail

# ANSI color codes for terminal output
C_RESET=$'\033[0m'
C_BOLD=$'\033[1m'
C_RED=$'\033[31m'
C_GREEN=$'\033[32m'
C_YELLOW=$'\033[33m'
C_BLUE=$'\033[34m'
C_MAGENTA=$'\033[35m'
C_CYAN=$'\033[36m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT_FILE="${1:-$SCRIPT_DIR/response.md}"
INPUT_FILE_JSON="${2:-$SCRIPT_DIR/response.json}"
REPORTS_DIR="$SCRIPT_DIR/reports"
PDF_DIR="$REPORTS_DIR/pdf"
MD_DIR="$REPORTS_DIR/md"
# TIMESTAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
CSS_FILE="$SCRIPT_DIR/styles.css"
PROMPT_ORG_FILE="$SCRIPT_DIR/prompts/content.md"

if [[ ! -f "$PROMPT_ORG_FILE" ]]; then
  echo "${C_RED}Prompt file not found: $PROMPT_ORG_FILE${C_RESET}" >&2
  exit 1
fi
PROMPT_ORG_ID=$(head -n 1 "$PROMPT_ORG_FILE" | sed -E 's/.*\(ID:\s*(.*?)\s*\).*/\1/')
echo "${C_CYAN}### Prompt ID:${C_RESET} ${C_BOLD}'$PROMPT_ORG_ID'${C_RESET}"

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "${C_RED}Input file not found: $INPUT_FILE${C_RESET}" >&2
  exit 1
fi

if [[ ! -f "$INPUT_FILE_JSON" ]]; then
  echo "${C_RED}Input file not found: $INPUT_FILE_JSON${C_RESET}" >&2
  exit 1
fi

# jsonlint -q "$INPUT_FILE_JSON" || { echo "Invalid JSON in $INPUT_FILE_JSON" >&2; exit 1; }
model=$(jq -r '.model' "$INPUT_FILE_JSON")
created=$(jq -r '.created' "$INPUT_FILE_JSON")
jq -r '.usage' "$INPUT_FILE_JSON"

CREATED=$(TZ="Europe/Berlin" date -d @$created +'%d-%m-%Y %H:%M:%S (%z)')
TIMESTAMP=$(TZ="Europe/Berlin" date -d @$created +'%Y%m%dT%H%M%SZ')
TITLE="ID: $PROMPT_ORG_ID, Model: $model, Created: $CREATED"
echo "${C_BLUE}Title:${C_RESET} ${C_BOLD}$TITLE${C_RESET}"
echo '------------------------------'

OUTPUT_PDF="$PDF_DIR/${PROMPT_ORG_ID}_${TIMESTAMP}_response.pdf"
OUTPUT_HTML="$PDF_DIR/${PROMPT_ORG_ID}_${TIMESTAMP}_response.html"
OUTPUT_MD="$MD_DIR/${PROMPT_ORG_ID}_${TIMESTAMP}_response.md"
OUTPUT_PROMPT="$MD_DIR/${PROMPT_ORG_ID}_${TIMESTAMP}_prompt.md"

mkdir -p "$REPORTS_DIR" "$PDF_DIR" "$MD_DIR"
cp "$INPUT_FILE" "$OUTPUT_MD"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

TRANSFORMED_INPUT="$TMP_DIR/response.transformed.md"
python3 "$SCRIPT_DIR/genpdf_quote_transform.py" "$INPUT_FILE" "$TRANSFORMED_INPUT"

pandoc "$TRANSFORMED_INPUT" \
  --from markdown+raw_html \
  --standalone \
  --metadata title="$TITLE" \
  --metadata charset="utf-8" \
  --css "$CSS_FILE" \
  -t html5 \
  -o "$OUTPUT_HTML"

if command -v weasyprint >/dev/null 2>&1; then
  weasyprint \
    --stylesheet "$CSS_FILE" \
    "$OUTPUT_HTML" "$OUTPUT_PDF"
else
  echo "${C_RED}WeasyPrint not available; please install it to generate PDFs.${C_RESET}" >&2
  exit 1
fi

{ 
  echo -e "\n=== $TITLE  ===\n"
  echo -e "\n=== CONTENT ===\n"
  cat ./prompts/compressed/content.md
  echo -e "\n=== SYSTEM ===\n"
  cat ./prompts/compressed/system.md
} > "$OUTPUT_PROMPT"

echo "${C_GREEN}PDF saved to:${C_RESET} $OUTPUT_PDF"
echo "${C_GREEN}Markdown copy saved to:${C_RESET} $OUTPUT_MD"
echo "${C_GREEN}HTML copy saved to:${C_RESET} $OUTPUT_HTML"
echo "${C_GREEN}Prompt copy saved to:${C_RESET} $OUTPUT_PROMPT"

# cp ./prompts/content.md "$MD_DIR/${TIMESTAMP}_prompts_content.md"
# cp ./prompts/objective.md "$MD_DIR/${TIMESTAMP}_prompts_objective.md"
# cp ./prompts/system.md "$MD_DIR/${TIMESTAMP}_prompts_system.md"
