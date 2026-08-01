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

jsonlint -q "$INPUT_FILE_JSON" || { echo "Invalid JSON in $INPUT_FILE_JSON" >&2; exit 1; }
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
CSS_FILE="$TMP_DIR/styles.css"
cat > "$CSS_FILE" <<'EOF'
@page {
  size: A4;
  margin: 0.9cm;
}

html {
  font-size: 100%;
}

body {
  font-family: "DejaVu Sans", "Noto Naskh Arabic", "Amiri", "Arial Unicode MS", Arial, sans-serif;
  font-size: 14.5pt;
  line-height: 1.58;
  color: #1f2933;
  margin: 0;
  text-align: justify;
}

h1, h2, h3, h4, h5, h6 {
  color: #102a43;
  margin-top: 1.05em;
  margin-bottom: 0.45em;
  line-height: 1.2;
}

h1.title, h1 {
  font-size: 24pt;
  border-bottom: 2px solid #d9e2ec;
  padding-bottom: 0.25em;
  margin-top: 0.2em;
  margin-bottom: 0.65em;
}

h2 {
  font-size: 18pt;
  border-bottom: 1px solid #e5e7eb;
  padding-bottom: 0.15em;
}

h3 { font-size: 15pt; }

p {
  margin: 0.55em 0;
  orphans: 3;
  widows: 3;
}

ul, ol {
  padding-left: 1.5em;
  margin: 0.6em 0;
}

li {
  margin: 0.25em 0;
}

table {
  width: 100%;
  max-width: 100%;
  border-collapse: collapse;
  font-size: 12.5pt;
  table-layout: auto;
  page-break-inside: auto;
  margin: 0.7em 0 0.9em;
}

tr {
  page-break-inside: avoid;
  page-break-after: auto;
}

th, td {
  border: 1px solid #cbd5e1;
  padding: 0.45em 0.55em;
  text-align: left;
  vertical-align: top;
}

th {
  background-color: #f1f5f9;
  font-weight: 600;
}

tr:nth-child(even) td {
  background-color: #fcfdff;
}

blockquote.report-quote {
  margin: 0.8em 0;
  padding: 0.55em 0.8em 0.55em 1em;
  border-right: 4px solid #d9e2ec;
  border-left: none;
  border-radius: 0 4px 4px 0;
  color: #486581;
  background: #f8fafc;
  display: block;
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
  overflow: hidden;
  overflow-wrap: break-word;
  word-wrap: break-word;
  white-space: normal;
  hyphens: auto;
  text-align: right;
  direction: rtl;
}

blockquote.report-quote .report-quote-body {
  display: block;
  width: 100%;
  max-width: 100%;
  overflow-wrap: break-word;
  word-wrap: break-word;
  white-space: normal;
  hyphens: auto;
  text-align: right;
  direction: rtl;
}

blockquote.report-quote p,
blockquote.report-quote ul,
blockquote.report-quote ol {
  margin: 0.25em 0;
  display: block;
  width: 100%;
  max-width: 100%;
  overflow-wrap: break-word;
  word-wrap: break-word;
  white-space: normal;
  hyphens: auto;
  text-align: right;
  direction: rtl;
}

code,
pre {
  font-family: "DejaVu Sans Mono", Consolas, monospace;
  background: #f8fafc;
}

pre {
  padding: 0.6em;
  border-radius: 4px;
}

img {
  max-width: 100%;
  height: auto;
}

* {
  box-sizing: border-box;
}
EOF

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
