#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT_FILE="${1:-$SCRIPT_DIR/response.md}"
REPORTS_DIR="$SCRIPT_DIR/reports"
PDF_DIR="$REPORTS_DIR/pdf"
MD_DIR="$REPORTS_DIR/md"
TIMESTAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
OUTPUT_FILE="$PDF_DIR/${TIMESTAMP}_response.pdf"
MARKDOWN_OUTPUT="$MD_DIR/${TIMESTAMP}_response.md"

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "Input file not found: $INPUT_FILE" >&2
  exit 1
fi

mkdir -p "$REPORTS_DIR" "$PDF_DIR" "$MD_DIR"
cp "$INPUT_FILE" "$MARKDOWN_OUTPUT"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

TRANSFORMED_INPUT="$TMP_DIR/response.transformed.md"
python3 "$SCRIPT_DIR/genpdf_quote_transform.py" "$INPUT_FILE" "$TRANSFORMED_INPUT"

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
  font-family: "DejaVu Sans", Arial, sans-serif;
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
  word-break: break-word;
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
  word-break: break-word;
  line-break: loose;
  white-space: normal;
  hyphens: auto;
  -webkit-hyphens: auto;
  text-align: start;
  direction: inherit;
  unicode-bidi: plaintext;
}

blockquote.report-quote .report-quote-body {
  display: block;
  width: 100%;
  max-width: 100%;
  overflow-wrap: break-word;
  word-wrap: break-word;
  word-break: break-word;
  line-break: loose;
  white-space: normal;
  hyphens: auto;
  -webkit-hyphens: auto;
  text-align: start;
  direction: inherit;
  unicode-bidi: plaintext;
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
  word-break: break-word;
  line-break: loose;
  white-space: normal;
  hyphens: auto;
  -webkit-hyphens: auto;
  text-align: start;
  direction: inherit;
  unicode-bidi: plaintext;
}

code,
pre {
  font-family: "DejaVu Sans Mono", Consolas, monospace;
  background: #f8fafc;
}

pre {
  padding: 0.6em;
  overflow-x: auto;
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
  --metadata title="MCP-Bridge Response" \
  --pdf-engine=wkhtmltopdf \
  --pdf-engine-opt=--page-size \
  --pdf-engine-opt=A4 \
  --pdf-engine-opt=--margin-top \
  --pdf-engine-opt=0.9cm \
  --pdf-engine-opt=--margin-bottom \
  --pdf-engine-opt=0.9cm \
  --pdf-engine-opt=--margin-left \
  --pdf-engine-opt=1.0cm \
  --pdf-engine-opt=--margin-right \
  --pdf-engine-opt=1.0cm \
  --pdf-engine-opt=--zoom \
  --pdf-engine-opt=1.12 \
  --css "$CSS_FILE" \
  -o "$OUTPUT_FILE"

echo "PDF saved to: $OUTPUT_FILE"
echo "Markdown copy saved to: $MARKDOWN_OUTPUT"
