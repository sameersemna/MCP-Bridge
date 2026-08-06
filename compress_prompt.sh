#!/bin/bash
set -euo pipefail

# python compress_prompt.py ./prompts/content.md --method hybrid    --model qwen2.5:3b  --output ./tmp/compressed.md
# python compress_prompt.py ./prompts/content.md --method llm       --model qwen2.5:3b  --output ./tmp/compressed.md
# python compress_prompt.py ./prompts/content.md --method caveman   --model qwen2.5:3b  --output ./tmp/compressed.md
# python compress_prompt.py ./prompts/content.md --method heuristic --level ultra  --output ./tmp/compressed.md
# python compress_prompt.py ./prompts/content.md --method hybrid    --level wenyan --model qwen2.5:3b  --output ./tmp/compressed.md

echo "Compressing prompts..."
python compress_prompt.py ./prompts/content.md --method hybrid --model qwen2.5:3b  --output ./prompts/compressed/content.md
# echo "Compressing system prompt..."
# python compress_prompt.py ./prompts/system.md --method hybrid --model qwen2.5:3b  --output ./prompts/compressed/system.md
echo "Done compressing prompts. Compressed prompts saved to ./prompts/compressed/content.md and ./prompts/compressed/system.md"
