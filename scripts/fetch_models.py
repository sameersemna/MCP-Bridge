#!/usr/bin/env python3
"""Fetch the OpenRouter model catalog into a local JSON file.

The bridge and the test harness scripts read this catalog to get per-model
context windows, pricing, and other details instead of relying on hardcoded
values. OpenRouter adds/removes models frequently, so re-run this script
periodically (or from CI) to keep the catalog current.

Usage:
    python scripts/fetch_models.py [--output models.json] [--base-url URL] [--api-key KEY]

The base URL and API key default to the values in config.json (if present),
falling back to the public OpenRouter endpoint (which needs no key for the
models list).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Default to the public endpoint; the models list does not require a key.
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "models.json"


def _load_config_base_url() -> str | None:
    """Read base_url from config.json if present."""
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cfg.get("inference_server", {}).get("base_url")
    except Exception:
        return None


def _load_config_api_key() -> str | None:
    """Read api_key from config.json if present."""
    config_path = Path(__file__).resolve().parent.parent / "config.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        key = cfg.get("inference_server", {}).get("api_key")
        return key if key and key not in {"None", "unauthenticated"} else None
    except Exception:
        return None


def fetch_models(base_url: str, api_key: str | None) -> list[dict]:
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    data = payload.get("data", [])
    if not isinstance(data, list):
        raise ValueError(f"Unexpected /models response shape: {list(payload.keys())}")
    return data


def build_catalog(models: list[dict]) -> dict:
    """Normalize the raw OpenRouter model list into a compact catalog keyed by model id."""
    catalog: dict[str, dict] = {}
    for model in models:
        mid = model.get("id")
        if not mid:
            continue
        pricing = model.get("pricing") or {}
        arch = model.get("architecture") or {}
        top_provider = model.get("top_provider") or {}
        catalog[mid] = {
            "name": model.get("name"),
            "context_length": model.get("context_length"),
            "max_completion_tokens": top_provider.get("max_completion_tokens"),
            "modality": arch.get("modality"),
            "input_modalities": arch.get("input_modalities"),
            "output_modalities": arch.get("output_modalities"),
            "prompt_price": pricing.get("prompt"),
            "completion_price": pricing.get("completion"),
            "input_cache_read_price": pricing.get("input_cache_read"),
            "knowledge_cutoff": model.get("knowledge_cutoff"),
            "reasoning": bool(model.get("reasoning")),
        }
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()

    base_url = args.base_url or _load_config_base_url() or DEFAULT_BASE_URL
    api_key = args.api_key or _load_config_api_key()

    print(f"Fetching models from {base_url}/models ...", file=sys.stderr)
    models = fetch_models(base_url, api_key)
    catalog = build_catalog(models)

    document = {
        "source": base_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "model_count": len(catalog),
        "models": catalog,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"Wrote {len(catalog)} models to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
