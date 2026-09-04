#!/usr/bin/env python3
"""Manual smoke test: does the model actually call the right MCP tool?

This fires a handful of natural-language prompts at a *running* mcp-bridge
instance and checks the per-request trace JSON (written by
`RequestTraceLogger` to `MCP_BRIDGE_LOG_DIR`, default `./logs` via the
`compose.yml` bind mount) for a `mcp_tool_calls` event naming a tool that
matches what we'd expect for that prompt.

This is deliberately NOT part of the `pytest` suite. Whether a model calls a
tool for a given prompt is the model's own (non-deterministic) judgment call,
not bridge code -- it depends on the live model, the live MCP servers
currently configured, and network access to them. Run it by hand after
config/model changes to spot-check that tool selection still works, e.g.:

    python scripts/smoke_test_tool_selection.py
    python scripts/smoke_test_tool_selection.py --case weather --model minimax/minimax-m3:free

The built-in sample cases below were checked against the actual tool names
this repo's `config.json` servers expose (via `list_tools()`), not guessed:
`google_weather` (google-search server), `search` (duckduckgo-search),
`calculate` (abacus), `search_papers` (arxiv). If your `config.json` differs,
edit SAMPLE_CASES to match your own configured servers.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "http://localhost:11410/v1"
DEFAULT_LOG_DIR = REPO_ROOT / "logs"
DEFAULT_MODEL = "minimax/minimax-m3:free"

# Each case: a prompt that plausibly needs a tool, and the substrings (case
# insensitive) we expect to find in *some* invoked tool's name. An empty
# `expect_substrings` just asserts that at least one tool was called at all.
SAMPLE_CASES = [
    {
        "name": "weather",
        "prompt": "What's the current weather in Berlin, Germany right now?",
        "expect_substrings": ["weather"],
    },
    {
        "name": "web_search",
        "prompt": "Search the web for the latest news about SpaceX Starship launches.",
        "expect_substrings": ["search"],
    },
    {
        "name": "calculator",
        "prompt": "What is 3892 multiplied by 194.5? Use a calculator tool, don't estimate.",
        "expect_substrings": ["calculate"],
    },
    {
        "name": "arxiv_papers",
        "prompt": "Find recent arXiv papers about transformer attention mechanisms.",
        "expect_substrings": ["search_papers", "arxiv"],
    },
]


def _sanitize_path(path: str) -> str:
    # Mirrors RequestTraceLogger._sanitize_path exactly, so trace filenames
    # for this endpoint can be located without importing the app.
    sanitized = path.strip("/").replace("/", "__") or "root"
    return sanitized.replace(" ", "_")


def send_prompt(base_url: str, model: str, prompt: str, timeout: float) -> dict:
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def find_trace_file(log_dir: Path, since: float) -> Path | None:
    """Return the newest .../v1/chat/completions trace file written at or
    after `since` (a few seconds of slack for clock/flush skew), or None."""
    suffix = _sanitize_path("/v1/chat/completions")
    candidates = [
        p for p in log_dir.glob(f"*_{suffix}.json") if p.stat().st_mtime >= since - 2
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def called_tool_names(trace_path: Path) -> list[str]:
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    names: list[str] = []
    for event in payload.get("events", []):
        if event.get("type") == "mcp_tool_calls":
            for call in event.get("tool_calls", []):
                name = call.get("name")
                if name:
                    names.append(str(name))
    return names


def run_case(case: dict, base_url: str, model: str, log_dir: Path, timeout: float) -> bool:
    print(f"\n=== {case['name']} ===")
    print(f"prompt: {case['prompt']}")

    start = time.time()
    try:
        response = send_prompt(base_url, model, case["prompt"], timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"ERROR: HTTP {exc.code}: {body[:500]}")
        return False
    except Exception as exc:
        print(f"ERROR: request failed: {type(exc).__name__}: {exc}")
        return False

    content = ""
    try:
        content = response["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        pass
    print(f"answer preview: {content[:200]!r}")

    trace_path = find_trace_file(log_dir, since=start)
    if trace_path is None:
        print(f"FAIL: no trace file found under {log_dir} for this request")
        return False

    tools_called = called_tool_names(trace_path)
    print(f"tools called: {tools_called or '(none)'}")
    print(f"trace file: {trace_path}")

    expected = [s.lower() for s in case.get("expect_substrings", [])]
    if not expected:
        ok = bool(tools_called)
    else:
        ok = any(sub in name.lower() for name in tools_called for sub in expected)

    print("PASS" if ok else f"FAIL: expected a tool matching {expected or '(any)'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-request timeout in seconds")
    parser.add_argument("--case", action="append", dest="cases", help="Run only this case name (repeatable)")
    args = parser.parse_args()

    cases = SAMPLE_CASES
    if args.cases:
        wanted = set(args.cases)
        cases = [c for c in SAMPLE_CASES if c["name"] in wanted]
        missing = wanted - {c["name"] for c in cases}
        if missing:
            print(f"unknown case name(s): {sorted(missing)}", file=sys.stderr)
            return 2

    if not args.log_dir.exists():
        print(f"log dir {args.log_dir} does not exist -- is mcp-bridge running with the logs volume mounted?", file=sys.stderr)
        return 2

    results = [run_case(case, args.base_url, args.model, args.log_dir, args.timeout) for case in cases]

    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
