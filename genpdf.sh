#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT_FILE="${1:-$SCRIPT_DIR/response.md}"
INPUT_FILE_JSON="${2:-$SCRIPT_DIR/response.json}"
REPORTS_DIR="$SCRIPT_DIR/reports"
PDF_DIR="$REPORTS_DIR/pdf"
MD_DIR="$REPORTS_DIR/md"
# TIMESTAMP="$(date -u +"%Y%m%dT%H%M%SZ")"

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
TITLE="Model: $model, Created: $CREATED"
echo "Title: $TITLE"
echo '------------------------------'

OUTPUT_FILE="$PDF_DIR/${TIMESTAMP}_response.pdf"
MARKDOWN_OUTPUT="$MD_DIR/${TIMESTAMP}_response.md"

mkdir -p "$REPORTS_DIR" "$PDF_DIR" "$MD_DIR"
cp "$INPUT_FILE" "$MARKDOWN_OUTPUT"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

TRANSFORMED_INPUT="$TMP_DIR/response.transformed.md"
python3 "$SCRIPT_DIR/genpdf_quote_transform.py" "$INPUT_FILE" "$TRANSFORMED_INPUT"

HTML_OUTPUT="$PDF_DIR/${TIMESTAMP}_response.html"
CSS_FILE="/home/sameer/Public/Shared/Work/Projects/MCP/MCP-Bridge/styles.css"

pandoc "$TRANSFORMED_INPUT" \
  --from markdown+raw_html \
  --standalone \
  --metadata title="$TITLE" \
  --metadata charset="utf-8" \
  --css "$CSS_FILE" \
  -t html5 \
  -o "$HTML_OUTPUT"

if command -v weasyprint >/dev/null 2>&1; then
  weasyprint \
    --stylesheet "$CSS_FILE" \
    "$HTML_OUTPUT" "$OUTPUT_FILE"
else
  echo "WeasyPrint not available; please install it to generate PDFs." >&2
  exit 1
fi

echo "PDF saved to: $OUTPUT_FILE"
echo "Markdown copy saved to: $MARKDOWN_OUTPUT"
echo "HTML copy saved to: $HTML_OUTPUT"
