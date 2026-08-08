#!/usr/bin/env python3
"""
compress_prompt.py - Semantic text compressor for LLM prompts.

Takes a text file as input, compresses its content without changing the meaning,
and saves the compressed result to a new text file.

Compression methods:
  1. llm      - Uses an LLM (NVIDIA API or Ollama) to rewrite/compress the text
  2. caveman  - Caveman-style prompt compression (inspired by JuliusBrussee/caveman)
  3. heuristic - Rule-based compression: removes filler words, simplifies sentences
  4. hybrid   - Heuristic pre-processing followed by LLM refinement

Similar projects that inspired this tool:
  - https://github.com/JuliusBrussee/caveman        (65% fewer output tokens via agent skill)
  - https://github.com/JuliusBrussee/caveman-code   (full terminal coding agent, caveman top to bottom)
  - https://github.com/JuliusBrussee/cavemem        (compresses what the agent remembers)
  - https://github.com/therealmoronto/claude-semantic-compression (Ovchinnikov Effect, 80% savings)
  - https://github.com/jiawei686/tokencompress      (local Ollama semantic + lossless gzip)
  - https://github.com/microsoft/LLMLingua          (token probability-based compression)

Usage:
  python compress_prompt.py <input_file> [options]

Examples:
  python compress_prompt.py prompt.txt
  python compress_prompt.py prompt.txt --method llm --model deepseek-v4-flash:cloud
  python compress_prompt.py prompt.txt --method caveman --output compressed.txt
  python compress_prompt.py prompt.txt --method heuristic --level ultra
  python compress_prompt.py prompt.txt --method hybrid
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_PORT = 11410
DEFAULT_OLLAMA_PORT = 11434
DEFAULT_NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "deepseek-ai/deepseek-v4-flash"
DEFAULT_NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "")
DEFAULT_TEMPERATURE = 0.1
DEFAULT_SEED = 42
DEFAULT_MAX_TOKENS = 4096


# ──────────────────────────────────────────────────────────────────────────────
# Heuristic compression (no LLM needed)
# ──────────────────────────────────────────────────────────────────────────────

# Common filler phrases and words that can be removed without changing meaning
FILLER_PHRASES = [
    # Verbose starters (order: longer patterns first)
    r"\bIt is important to note that\b",
    r"\bIt is worth noting that\b",
    r"\bIt should be noted that\b",
    r"\bIt's worth mentioning that\b",
    r"\bTaking into account the fact that\b",
    r"\bIn light of the fact that\b",
    r"\bBecause of the fact that\b",
    r"\bDue to the fact that\b",
    r"\bFor the reason that\b",
    r"\bOn the grounds that\b",
    r"\bIn the neighborhood of\b",
    r"\bI would like to point out that\b",
    r"\bI want to emphasize that\b",
    r"\bIt goes without saying that\b",
    r"\bAs a matter of fact\b",
    r"\bIn order to\b",
    r"\bFor the purpose of\b",
    r"\bWith regard to\b",
    r"\bWith respect to\b",
    r"\bIn the event that\b",
    r"\bIn the case of\b",
    r"\bAt this point in time\b",
    r"\bAt the present time\b",
    r"\bOn a daily basis\b",
    r"\bOn a regular basis\b",
    r"\bIn a timely manner\b",
    r"\bIn the near future\b",
    r"\bGiven the fact that\b",
    r"\bIt can be said that\b",
    r"\bIt can be seen that\b",
    r"\bIt is clear that\b",
    r"\bIt is evident that\b",
    r"\bIt is obvious that\b",
    r"\bAs you may be aware\b",
    r"\bAs we all know\b",
    r"\bIn terms of\b",
    r"\bWith a view to\b",
    r"\bIn connection with\b",
    r"\bIn the neighborhood of\b",
    r"\bEach and every\b",
    r"\bFirst and foremost\b",
    r"\bLast but not least\b",
    r"\bThe majority of\b",
    r"\bA large number of\b",
    r"\bA wide variety of\b",
    r"\bIn essence\b",
    r"\bFundamentally speaking\b",
    r"\bBasically speaking\b",
    r"\bFrom my perspective\b",
    r"\bIn my opinion\b",
    r"\bI personally believe\b",
    r"\bThe reason for this is\b",
    r"\bThis is due to the fact\b",
    r"\bBasically what this means is\b",
    r"\bThe bottom line is\b",
    r"\bAt the end of the day\b",
    r"\bWhen all is said and done\b",
    r"\bIn today's modern world\b",
    r"\bIn the modern era\b",
    r"\bGoing forward\b",
    r"\bMoving forward\b",
    r"\bHaving said that\b",
    r"\bThat being said\b",
    r"\bIn summary\b",
    r"\bTo summarize\b",
    r"\bIn conclusion\b",
    r"\bTo conclude\b",
    # Sentence-level hedging and fluff starters
    r"\byou will need to follow the following steps carefully\.?\s*",
    r"\byou should be aware that\b",
    r"\byou should take into account the fact that\b",
    r"\byou need to modify\b",
    r"\byou should\b",
    r"\byou will\b",
    r"\bwhich means they\b",
    r"\bwhich can be found in\b",
    r"\bthat can significantly\b",
    r"\bthat serves as a crucial\b",
    r"\bthat enables\b",
    r"\bwhich is perhaps the simplest and most straightforward approach\b",
    r"\bwhich translates directly to\b",
    r"\bwhile trying to preserve\b",
    r"\bwhile the\b",
    r"\bbut it may not\b",
    r"\bwhich may incur\b",
    r"\bby leveraging\b",
]

FILLER_WORDS = [
    r"\bvery\b",
    r"\breally\b",
    r"\bquite\b",
    r"\brather\b",
    r"\bactually\b",
    r"\bbasically\b",
    r"\bliterally\b",
    r"\bsimply\b",
    r"\bjust\b",
    r"\bperhaps\b",
    r"\bmaybe\b",
    r"\bpossibly\b",
    r"\bprobably\b",
    r"\bdefinitely\b",
    r"\bcertainly\b",
    r"\babsolutely\b",
    r"\bcompletely\b",
    r"\btotally\b",
    r"\bentirely\b",
    r"\benhance\b",
    r"\bleverage\b",
    r"\bfacilitate\b",
    r"\bprior to\b",
    r"\bsubsequent to\b",
    r"\bpursuant to\b",
    r"\butilize\b",
    r"\bimplement\b",
    r"\bcommence\b",
    r"\bterminate\b",
    r"\bsignificantly\b",
    r"\bcrucial\b",
    r"\bversatile\b",
    r"\bnumerous\b",
    r"\bsubstantial\b",
    r"\bextensive\b",
    r"\bstraightforward\b",
    r"\bsuccessfully\b",
]

# Compression levels determine aggressiveness
COMPRESSION_LEVELS = {
    "lite": {
        "remove_filler_phrases": True,
        "remove_filler_words": False,
        "simplify_connectives": True,
        "compress_lists": False,
        "remove_redundancy": True,
        "sentence_simplify": False,
        "merge_sentences": False,
        "remove_articles": False,
    },
    "full": {
        "remove_filler_phrases": True,
        "remove_filler_words": True,
        "simplify_connectives": True,
        "compress_lists": True,
        "remove_redundancy": True,
        "sentence_simplify": True,
        "merge_sentences": False,
        "remove_articles": False,
    },
    "ultra": {
        "remove_filler_phrases": True,
        "remove_filler_words": True,
        "simplify_connectives": True,
        "compress_lists": True,
        "remove_redundancy": True,
        "sentence_simplify": True,
        "merge_sentences": True,
        "remove_articles": True,
    },
}

# Connective simplification mappings
CONNECTIVE_MAP = {
    r"\bHowever,?\s*": "But ",
    r"\bFurthermore,?\s*": "",
    r"\bMoreover,?\s*": "",
    r"\bAdditionally,?\s*": "",
    r"\bNevertheless,?\s*": "Still, ",
    r"\bConsequently,?\s*": "So ",
    r"\bTherefore,?\s*": "So ",
    r"\bSubsequently,?\s*": "Then ",
    r"\bMeanwhile,?\s*": "",
    r"\bAlternatively,?\s*": "Or ",
    r"\bIn addition,?\s*": "",
    r"\bAs a result,?\s*": "So ",
    r"\bFor example,?\s*": "E.g., ",
    r"\bFor instance,?\s*": "E.g., ",
    r"\bIn other words,?\s*": "",
    r"\bThat is to say,?\s*": "",
    r"\bOn the other hand,?\s*": "But ",
    r"\bIn contrast,?\s*": "But ",
    r"\bSimilarly,?\s*": "",
    r"\bLikewise,?\s*": "",
}

# Redundancy patterns (pairs where one word makes the other redundant)
REDUNDANCY_PATTERNS = [
    (r"\bfinal result\b", "result"),
    (r"\bpast history\b", "history"),
    (r"\bfuture plans\b", "plans"),
    (r"\bcompletely finished\b", "finished"),
    (r"\btruly unique\b", "unique"),
    (r"\bbasic fundamentals\b", "fundamentals"),
    (r"\btrue facts\b", "facts"),
    (r"\bpast memories\b", "memories"),
    (r"\bunexpected surprise\b", "surprise"),
    (r"\bfree gift\b", "gift"),
    (r"\badvance planning\b", "planning"),
    (r"\bend result\b", "result"),
    (r"\breverse back\b", "reverse"),
    (r"\brevert back\b", "revert"),
    (r"\bclose proximity\b", "proximity"),
    (r"\bround circle\b", "circle"),
    (r"\bsquare box\b", "box"),
    (r"\babsolutely essential\b", "essential"),
    (r"\babsolutely necessary\b", "necessary"),
    (r"\btotally complete\b", "complete"),
    (r"\bnull and void\b", "void"),
    (r"\bcease and desist\b", "stop"),
    (r"\bfirst and foremost\b", "first"),
    (r"\beach and every\b", "every"),
]


def heuristic_compress(text: str, level: str = "full") -> str:
    """Compress text using heuristic rules (no LLM required)."""
    settings = COMPRESSION_LEVELS.get(level, COMPRESSION_LEVELS["full"])
    result = text

    # Remove filler phrases
    if settings["remove_filler_phrases"]:
        for phrase in FILLER_PHRASES:
            result = re.sub(phrase, "", result, flags=re.IGNORECASE)

    # Remove filler words
    if settings["remove_filler_words"]:
        for word in FILLER_WORDS:
            result = re.sub(word, "", result, flags=re.IGNORECASE)

    # Simplify connectives
    if settings["simplify_connectives"]:
        for pattern, replacement in CONNECTIVE_MAP.items():
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    # Remove redundancy
    if settings["remove_redundancy"]:
        for pattern, replacement in REDUNDANCY_PATTERNS:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    # Sentence simplification
    if settings["sentence_simplify"]:
        # Collapse multiple sentences connected by "and" where possible
        result = re.sub(r"\.\s*And\b", ".", result)
        # Remove "that" after certain verbs
        result = re.sub(r"\b(is|are|was|were|seems|appears) that\b", r"\1", result)
        # Simplify "which is" constructions
        result = re.sub(r"\bwhich is\b", "that's", result)
        # Remove passive voice fluff: "it is used to" → just the verb
        result = re.sub(r"\bIt is (?:used to|designed to|intended to)\b", "This", result)
        # Collapse "the X that is Y" → "X=Y" style where safe
        result = re.sub(r"\bthe fact that\b", "", result)
        result = re.sub(r"\bthe way that\b", "how", result)
        result = re.sub(r"\bthe process of\b", "", result)
        result = re.sub(r"\bthe ability to\b", "can", result)
        result = re.sub(r"\bthe use of\b", "", result)

    # Merge sentences: join short sentences separated by periods
    if settings.get("merge_sentences"):
        # Collapse ". Sentence that " → "; sentence that " for tighter packing
        def _merge_short(m):
            prev = m.group(1).rstrip(".")
            nxt = m.group(2)
            # Only merge if next sentence is short and starts with lowercase
            if len(nxt) < 80 and nxt[0].islower():
                return f"{prev}; {nxt}"
            return f"{prev}. {nxt}"
        result = re.sub(r"\.\s+([A-Z])", lambda m: f". {m.group(1)}", result)
        # Remove leading articles ("a", "an", "the") when they add no meaning
    if settings.get("remove_articles"):
        # Remove leading "The " / "A " / "An " at start of a line
        result = re.sub(r"^The\s+(?=[a-z])", "", result, flags=re.MULTILINE)
        result = re.sub(r"^A\s+(?=[a-z])", "", result, flags=re.MULTILINE)
        result = re.sub(r"^An\s+(?=[a-z])", "", result, flags=re.MULTILINE)
        # Remove after period: ". The " → ". "
        result = re.sub(r"\.\s+The\s+(?=[a-z])", ". ", result)
        result = re.sub(r"\.\s+A\s+(?=[a-z])", ". ", result)
        result = re.sub(r"\.\s+An\s+(?=[a-z])", ". ", result)

    # Clean up whitespace artifacts
    result = re.sub(r"  +", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r"^[ \t]+|[ \t]+$", "", result, flags=re.MULTILINE)
    # Fix punctuation spacing
    result = re.sub(r"\s+([.,;:!?])", r"\1", result)
    result = re.sub(r"\.{2,}", ".", result)
    # Remove empty lines left by removed phrases
    lines = result.split("\n")
    cleaned_lines = []
    prev_empty = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not prev_empty:
                cleaned_lines.append("")
            prev_empty = True
        else:
            cleaned_lines.append(stripped)
            prev_empty = False
    result = "\n".join(cleaned_lines)

    return result.strip()


# ──────────────────────────────────────────────────────────────────────────────
# LLM-based compression
# ──────────────────────────────────────────────────────────────────────────────

CAVEMAN_SYSTEM_PROMPT = textwrap.dedent("""\
You are a text compressor. Your job is to compress the given text into a
semantically equivalent but much shorter version, suitable for use as an LLM
prompt. Preserve ALL factual content, technical details, code references,
URLs, names, and specific data. Remove filler words, redundancies, and
unnecessary explanations. Use fragments, abbreviations where unambiguous,
and compressed notation.

Rules:
- Keep all technical terms, proper nouns, code snippets, URLs, file paths
- Preserve the original language of the text
- Remove: pleasantries, apologies, hedging, meta-commentary, obvious inferences
- Use: fragments, abbreviations, semicolons, colons, short phrases
- Never add new information not in the original
- Target: 40-70% fewer tokens while preserving all key information

Output ONLY the compressed text. No explanation of what you did.""")

CAVEMAN_LITE_PROMPT = textwrap.dedent("""\
Compress this text for use as an LLM prompt. Remove filler, keep substance.
Use fragments, drop obvious inferences, preserve all technical details.
Output only compressed text.""")

CAVEMAN_ULTRA_PROMPT = textwrap.dedent("""\
Ultra-compress this text for LLM prompt use. Minimum words, maximum meaning.
Keep only: facts, names, numbers, code refs, URLs, constraints, goals.
Drop: everything else. Fragments only. Output only compressed text.""")

CAVEMAN_WENYAN_PROMPT = textwrap.dedent("""\
Compress this text to maximum density for LLM prompt use.
Use: abbreviations, symbols, semicolons, fragments.
Preserve: all technical content, names, URLs, code.
Drop: all prose padding, connective tissue, explanations.
Output only the compressed text.""")


LEVEL_PROMPTS = {
    "lite": CAVEMAN_LITE_PROMPT,
    "full": CAVEMAN_SYSTEM_PROMPT,
    "ultra": CAVEMAN_ULTRA_PROMPT,
    "wenyan": CAVEMAN_WENYAN_PROMPT,
}


def call_openai_compatible_api(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_content: str,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Call an OpenAI-compatible API (NVIDIA, Ollama, OpenRouter, etc.)."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            return content.strip()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        print(f"API error {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Request failed: {e}", file=sys.stderr)
        sys.exit(1)


def llm_compress(
    text: str,
    method: str,
    level: str = "full",
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    ollama_url: Optional[str] = None,
) -> str:
    """Compress text using an LLM."""

    # Determine the API endpoint
    if base_url is None:
        # Check if model contains "/" (NVIDIA style) or if Ollama is preferred
        if model and "/" in model:
            base_url = DEFAULT_NVIDIA_BASE
            api_key = api_key or DEFAULT_NVIDIA_KEY
        elif ollama_url:
            base_url = ollama_url
        else:
            base_url = f"http://localhost:{DEFAULT_PORT}/v1"

    if model is None:
        model = DEFAULT_MODEL

    if ollama_url is None:
        ollama_url = f"http://localhost:{DEFAULT_OLLAMA_PORT}"

    system_prompt = LEVEL_PROMPTS.get(level, LEVEL_PROMPTS["full"])

    if method == "caveman":
        # Use the caveman-specific system prompt
        pass  # system_prompt already set from LEVEL_PROMPTS
    elif method == "llm":
        # Generic compression prompt
        system_prompt = textwrap.dedent("""\
            You are a text compression expert. Compress the following text to be
            used as a prompt for an LLM. Preserve ALL meaning, facts, technical
            details, and key information. Remove redundancy, filler words, and
            unnecessary elaboration. Use concise language, fragments where
            appropriate. Output ONLY the compressed text, no commentary.""")
    elif method == "hybrid":
        # Hybrid already pre-processed with heuristic, just refine
        system_prompt = textwrap.dedent("""\
            Further compress this already-preserved text. Remove any remaining
            redundancy, tighten phrasing, use abbreviations where clear.
            Keep all technical content intact. Output only compressed text.""")

    # Check if using Ollama (local model with / suffix)
    if model and ":" in model and "/" not in model:
        base_url = f"{ollama_url}/v1"

    # If base_url points to the MCP bridge, check if model is available
    if base_url and "localhost" in base_url and "11410" in base_url:
        # Verify bridge is up
        try:
            health_url = f"http://localhost:{DEFAULT_PORT}/health"
            req = urllib.request.Request(health_url)
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            print(
                f"Warning: MCP Bridge not running on port {DEFAULT_PORT}. "
                "Falling back to Ollama.",
                file=sys.stderr,
            )
            base_url = f"http://localhost:{DEFAULT_OLLAMA_PORT}/v1"

    return call_openai_compatible_api(
        base_url=base_url,
        api_key=api_key or "",
        model=model,
        system_prompt=system_prompt,
        user_content=text,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Stats
# ──────────────────────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English."""
    return max(1, len(text) // 4)


def print_stats(original: str, compressed: str) -> None:
    """Print compression statistics."""
    orig_chars = len(original)
    comp_chars = len(compressed)
    orig_tokens = estimate_tokens(original)
    comp_tokens = estimate_tokens(compressed)
    char_pct = (1 - comp_chars / orig_chars) * 100 if orig_chars else 0
    token_pct = (1 - comp_tokens / orig_tokens) * 100 if orig_tokens else 0

    print("\n" + "─" * 50)
    print("  Compression Statistics")
    print("─" * 50)
    print(f"  Original:     {orig_chars:>8} chars  ~{orig_tokens:>5} tokens")
    print(f"  Compressed:   {comp_chars:>8} chars  ~{comp_tokens:>5} tokens")
    print(f"  Saved:        {char_pct:>7.1f}% chars    {token_pct:>5.1f}% tokens")
    print("─" * 50)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compress text for LLM prompts while preserving meaning.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s prompt.txt
              %(prog)s prompt.txt --method heuristic --level ultra
              %(prog)s prompt.txt --method llm --model deepseek-ai/deepseek-v4-flash
              %(prog)s prompt.txt --method caveman --level full
              %(prog)s prompt.txt --method hybrid --output compressed.txt

            Similar projects:
              github.com/JuliusBrussee/caveman       (65%% fewer output tokens)
              github.com/JuliusBrussee/cavemem        (compresses agent memory)
              github.com/microsoft/LLMLingua          (token prob compression)
              github.com/jiawei686/tokencompress      (local Ollama semantic)
              github.com/therealmoronto/claude-semantic-compression
        """),
    )
    parser.add_argument("input", help="Path to the input text file")
    parser.add_argument(
        "--output", "-o",
        help="Path to the output file (default: <input>_compressed.txt)",
    )
    parser.add_argument(
        "--method", "-m",
        choices=["llm", "caveman", "heuristic", "hybrid"],
        default="heuristic",
        help="Compression method (default: heuristic)",
    )
    parser.add_argument(
        "--level", "-l",
        choices=["lite", "full", "ultra", "wenyan"],
        default="full",
        help="Compression level (default: full)",
    )
    parser.add_argument("--model", "-M", help="LLM model name (for llm/caveman/hybrid)")
    parser.add_argument("--base-url", help="API base URL (default: auto-detect)")
    parser.add_argument("--api-key", help="API key (default: NVIDIA_API_KEY env var)")
    parser.add_argument("--ollama-url", help="Ollama URL (default: http://localhost:11434)")
    parser.add_argument(
        "--no-stats",
        action="store_true",
        help="Don't print compression statistics",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Read input file
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    original = input_path.read_text(encoding="utf-8")
    if not original.strip():
        print("Error: Input file is empty.", file=sys.stderr)
        sys.exit(1)

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(
            f"{input_path.stem}_compressed{input_path.suffix}"
        )

    print(f"Input:    {input_path} ({len(original)} chars)")
    print(f"Method:   {args.method}")
    print(f"Level:    {args.level}")

    # Compress
    if args.method == "heuristic":
        compressed = heuristic_compress(original, level=args.level)
    elif args.method in ("llm", "caveman", "hybrid"):
        text_to_compress = original
        if args.method == "hybrid":
            print("  Step 1/2: Heuristic pre-processing...")
            text_to_compress = heuristic_compress(original, level="full")
            print(f"  After heuristic: {len(text_to_compress)} chars")

        print(f"  {'Step 2/2' if args.method == 'hybrid' else 'Step 1/1'}: LLM compression...")
        compressed = llm_compress(
            text=text_to_compress,
            method=args.method,
            level=args.level,
            base_url=args.base_url,
            api_key=args.api_key,
            model=args.model,
            ollama_url=args.ollama_url,
        )
    else:
        print(f"Error: Unknown method '{args.method}'", file=sys.stderr)
        sys.exit(1)

    # Write output
    output_path.write_text(compressed, encoding="utf-8")
    print(f"Output:   {output_path} ({len(compressed)} chars)")

    # Stats
    if not args.no_stats:
        print_stats(original, compressed)

    print("Done.")


if __name__ == "__main__":
    main()