#!/bin/bash
set -euo pipefail

MODEL="qwen2.5:3b" # Dense
MODEL="qwen2.5:1.5b-instruct-q8_0" # Dense

# python compress_prompt.py ./prompts/cline.md --method hybrid    --model $MODEL  --output ./prompts/cline0.md
# python compress_prompt.py ./prompts/content.md --method hybrid    --model $MODEL  --output ./tmp/compressed.md
# python compress_prompt.py ./prompts/content.md --method llm       --model $MODEL  --output ./tmp/compressed.md
# python compress_prompt.py ./prompts/content.md --method caveman   --model $MODEL  --output ./tmp/compressed.md
# python compress_prompt.py ./prompts/content.md --method heuristic --level ultra  --output ./tmp/compressed.md
# python compress_prompt.py ./prompts/content.md --method hybrid    --level wenyan --model $MODEL  --output ./tmp/compressed.md

echo "Compressing prompts using Model: $MODEL ..."
python compress_prompt.py ./prompts/content.md --method hybrid --model $MODEL  --output ./prompts/compressed/content.md
# echo "Compressing system prompt..."
# python compress_prompt.py ./prompts/system.md --method hybrid --model $MODEL  --output ./prompts/compressed/system.md
echo "Done compressing prompts. Compressed prompts saved to ./prompts/compressed/content.md and ./prompts/compressed/system.md"
