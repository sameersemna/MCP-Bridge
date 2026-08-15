#!/usr/bin/env bash
set -euo pipefail

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
  echo "Prompt file not found: $PROMPT_ORG_FILE" >&2
  exit 1
fi
PROMPT_ORG_ID=$(head -n 1 "$PROMPT_ORG_FILE" | sed -E 's/.*\(ID:\s*(.*?)\s*\).*/\1/')
echo "### Prompt ID: '$PROMPT_ORG_ID'"

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "Input file not found: $INPUT_FILE" >&2
  exit 1
fi

if [[ ! -f "$INPUT_FILE_JSON" ]]; then
  echo "Input file not found: $INPUT_FILE_JSON" >&2
  exit 1
fi

# jsonlint -q "$INPUT_FILE_JSON" || { echo "Invalid JSON in $INPUT_FILE_JSON" >&2; exit 1; }
model=$(jq -r '.model' "$INPUT_FILE_JSON")
created=$(jq -r '.created' "$INPUT_FILE_JSON")
jq -r '.usage' "$INPUT_FILE_JSON"

CREATED=$(TZ="Europe/Berlin" date -d @$created +'%d-%m-%Y %H:%M:%S (%z)')
TIMESTAMP=$(TZ="Europe/Berlin" date -d @$created +'%Y%m%dT%H%M%SZ')
TITLE="ID: $PROMPT_ORG_ID, Model: $model, Created: $CREATED"
echo "Title: $TITLE"
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
  echo "WeasyPrint not available; please install it to generate PDFs." >&2
  exit 1
fi

{ 
  echo -e "\n=== $TITLE  ===\n"
  echo -e "\n=== CONTENT ===\n"
  cat ./prompts/compressed/content.md
  echo -e "\n=== SYSTEM ===\n"
  cat ./prompts/compressed/system.md
} > $OUTPUT_PROMPT

echo "PDF saved to: $OUTPUT_PDF"
echo "Markdown copy saved to: $OUTPUT_MD"
echo "HTML copy saved to: $OUTPUT_HTML"
echo "Prompt copy saved to: $OUTPUT_PROMPT"

# cp ./prompts/content.md "$MD_DIR/${TIMESTAMP}_prompts_content.md"
# cp ./prompts/objective.md "$MD_DIR/${TIMESTAMP}_prompts_objective.md"
# cp ./prompts/system.md "$MD_DIR/${TIMESTAMP}_prompts_system.md"
