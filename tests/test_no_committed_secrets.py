"""No tracked file may contain a credential-looking value (P0-01).

The scanner is intentionally a lint, not a proof: see
``scripts/check_secrets.py``. These tests pin both directions -- known-bad
formats are detected (non-vacuity) and placeholders are not (precision).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_secrets  # noqa: E402  (import after sys.path fix-up, as other tests do)


def test_scanner_detects_each_supported_format() -> None:
    """Every rule must fire on a synthetic value, built here so the file stays clean."""
    cases = {
        "context7": "ctx7" + "sk-" + "a" * 12,
        "tavily": "tv" + "ly-" + "a" * 12,
        "firecrawl": "fc" + "-" + "a" * 24,
        "openai": "sk" + "-" + "a" * 24,
        "github": "gh" + "p_" + "a" * 24,
        "aws-access-key": "AK" + "IA" + "A" * 16,
    }
    for expected_rule, value in cases.items():
        hits = check_secrets.find_secrets(f'key = "{value}"')
        assert (1, expected_rule) in hits, f"{expected_rule} value was not detected"


def test_placeholders_are_not_flagged() -> None:
    """The substitution this item performs must not itself trip the scanner."""
    samples = [
        '"CONTEXT7_API_KEY": "${CONTEXT7_API_KEY}"',
        '"TAVILY_API_KEY": "${env:TAVILY_API_KEY}"',
        'token = "<<REPLACE_ME>>"',
        "Authorization: <REDACTED>",
    ]
    for sample in samples:
        assert check_secrets.find_secrets(sample) == [], sample


def test_placeholder_does_not_mask_a_real_value_on_the_same_line() -> None:
    """A line may mix both; removing the placeholder span must not hide the secret."""
    real = "tv" + "ly-" + "b" * 12
    line = f'"A": "${{SAFE}}", "B": "{real}"'
    assert (1, "tavily") in check_secrets.find_secrets(line)


def test_tracked_tree_has_no_credentials() -> None:
    findings = check_secrets.scan(check_secrets.tracked_files())
    rendered = "\n".join(f"{path}:{line}: {rule}" for path, line, rule in findings)
    assert findings == [], (
        "Credential-looking values are tracked. Replace them with ${ENV_VAR} "
        f"placeholders and rotate the keys:\n{rendered}"
    )
