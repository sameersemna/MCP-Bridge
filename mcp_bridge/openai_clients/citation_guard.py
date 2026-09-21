"""Citation provenance guard.

Detects and flags URL citations in a generated response that cannot be traced
back to the tool evidence actually gathered during the request.

LLMs routinely fabricate plausible-looking URLs when a prompt demands sources
for claims they never verified. Observed in a real generated report:

    https://www.youtube.com/watch?v=abc123
    https://www.youtube.com/watch?v=def456
    https://www.facebook.com/<page>/posts/123456789
    https://books.google.com/books?id=XYZ
    https://scholar.google.com/scholar?cluster=12345

None of those exist. Worse, the report asserted "All sources are publicly
accessible" -- so a reader has no way to tell the citations are invented. Left
unchecked, this makes the whole system untrustworthy.

This module is the bridge's *defense in depth* against that class of failure.
It is deliberately conservative: it does not rewrite the model's answer or drop
claims -- it only (a) detects citations that were never retrieved and (b)
appends an explicit, clearly marked warning naming them, so the report is
*transparent* about which citations are untrustworthy instead of silently
presenting fabricated ones as authoritative.

Two independent signals are used:

1. **Provenance** -- a URL cited in the final answer that never appeared in the
   tool results (or tool-call arguments) for this request was not retrieved, so
   it is flagged as *unverified*. Only applied when tool evidence exists.
2. **Placeholder shape** -- a URL whose path/query/host contains an obvious
   placeholder token (``abc123``, ``XYZ``, ``12345``, ``example.com``, ...).
   Applied always, because such a URL is fake regardless of provenance.

Everything is opt-out via ``MCP_BRIDGE_CITATION_GUARD`` (default on).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

DEFAULT_CITATION_GUARD_ENABLED = True

# Matches http(s) URLs in text, stopping at whitespace, quotes, angle brackets,
# and closing brackets (so a URL inside ``(...)`` or ``[...]`` is clean).
_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+")

# Hosts that only ever appear in examples/tests, never in a real citation.
_PLACEHOLDER_HOSTS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "test.com",
        "test.example",
        "localhost",
        "your-domain.com",
    }
)

# Obvious placeholder tokens. Deliberately an explicit allow-list rather than a
# loose pattern like ``[a-z]{1,3}\d{1,3}`` (which would false-positive on real
# short ids such as ``?id=ab12``). These tokens appear when a model is "filling
# in" a URL it never obtained.
_PLACEHOLDER_TOKEN_RE = re.compile(
    r"(?:^|[/=&?_.\-])("
    r"abc123|def456|ghi789|jkl012|mno345|pqr678|stu901|vwx234|yz5678|"
    r"xyz|xxx|yyy|zzz|xxxx|yyyy|zzzz|"
    r"12345|123456|123456789|1234567890|00000|000000|99999|"
    r"placeholder|dummy|foobar|foobarbaz|sample(?:id)?|"
    r"your[_-]?(?:id|url|link|domain|page)|"
    r"insert[_-]?(?:id|url|link)|"
    r"tbd|todo|fixme"
    r")(?:$|[/=&?_.\-])",
    re.IGNORECASE,
)

# A URL whose *entire* last path/last query value is a bare ``XYZ``-style token
# (3 upper-case letters used as a stand-in). Matched separately because the
# delimiter-bounded regex above requires a separating character.
_BARE_PLACEHOLDER_RE = re.compile(
    r"(?:[/=&?])(xyz|abc|id|value)(?:$|[/&#])",
    re.IGNORECASE,
)

_UNVERIFIED_MARKER_PREFIX = "<!-- mcp-bridge:unverified-citations"


def get_citation_guard_enabled() -> bool:
    """Whether the bridge should annotate responses with citation warnings.

    Default on. Set ``MCP_BRIDGE_CITATION_GUARD=false`` to disable.
    """
    raw = os.getenv("MCP_BRIDGE_CITATION_GUARD")
    if raw is None:
        return DEFAULT_CITATION_GUARD_ENABLED
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_url(url: str) -> str:
    """Normalize a URL for provenance comparison.

    Collapses differences that do not change which source a URL points at:
    the scheme (``http`` vs ``https``), host case, a leading ``www.``, the
    fragment, and a trailing slash. Also strips trailing punctuation that
    sticks to a URL when it is embedded in prose. Two URLs that differ only in
    these ways are treated as the same source.
    """
    candidate = (url or "").strip().rstrip(".,;:!?").rstrip(")").rstrip("]")
    try:
        parts = urlsplit(candidate)
        netloc = (parts.netloc or "").lower()
        # Drop a leading ``www.`` so ``www.foo.com`` and ``foo.com`` match.
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parts.path or ""
        if path.endswith("/") and len(path) > 1:
            path = path.rstrip("/")
        # Scheme is deliberately dropped: ``http`` and ``https`` to the same
        # host+path are the same source for provenance purposes.
        return urlunsplit(("", netloc, path, parts.query, ""))
    except Exception:
        return candidate.lower()


def extract_urls(text: str | None) -> list[str]:
    """Return the distinct URLs in ``text``, preserving first-seen order."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for match in _URL_RE.findall(text):
        cleaned = match.rstrip(".,;:!?")
        if cleaned and cleaned not in seen:
            seen[cleaned] = None
    return list(seen)


def _message_evidence_text(message: Any) -> str:
    """Serialize a request message to text for URL evidence collection.

    Handles pydantic models and plain dicts, and includes *both* tool-result
    content and assistant tool-call arguments -- a URL the model passed to a
    fetch/search tool is evidence it saw that URL.
    """
    try:
        if hasattr(message, "model_dump"):
            dumped = message.model_dump(
                exclude_defaults=True, exclude_none=True, exclude_unset=True
            )
            return json.dumps(dumped, ensure_ascii=False, default=str)
    except Exception:
        pass
    try:
        return json.dumps(message, ensure_ascii=False, default=str)
    except Exception:
        return str(message)


def collect_evidence_urls(messages: Iterable[Any] | None) -> set[str]:
    """Collect the normalized URLs that actually appeared in the request's
    tool evidence (tool results and tool-call arguments)."""
    evidence: set[str] = set()
    for message in messages or []:
        text = _message_evidence_text(message)
        for raw in _URL_RE.findall(text or ""):
            evidence.add(normalize_url(raw))
    return evidence


def update_evidence_from_text(evidence: set[str], text: Any) -> None:
    """Add every URL found in ``text`` (normalized) to ``evidence``.

    Used by the tool loop to accumulate retrieved URLs *as tool results
    arrive*, so evidence survives later context compression (which may replace
    older tool messages with a URL-free summary).
    """
    if not text:
        return
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False, default=str)
        except Exception:
            text = str(text)
    for raw in _URL_RE.findall(text):
        evidence.add(normalize_url(raw))


def is_placeholder_url(url: str) -> bool:
    """Return True if ``url`` has an obvious placeholder shape."""
    if not url:
        return False
    try:
        parts = urlsplit(url)
    except Exception:
        return False

    host = (parts.netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host in _PLACEHOLDER_HOSTS:
        return True

    haystack = f"{parts.path}?{parts.query}" if parts.query else parts.path
    if _PLACEHOLDER_TOKEN_RE.search(haystack):
        return True
    if _BARE_PLACEHOLDER_RE.search(haystack):
        return True

    # A bare ``XYZ``-style token as the whole last path segment or query value.
    last_segment = (parts.path or "").rstrip("/").rsplit("/", 1)[-1]
    if last_segment and re.fullmatch(r"[A-Z]{2,4}", last_segment):
        return True

    return False


@dataclass
class CitationReport:
    """The result of analyzing a response's citations against tool evidence."""

    cited: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    evidence_available: bool = False

    @property
    def flagged(self) -> list[str]:
        """All URLs that should be called out, de-duplicated in cited order."""
        seen: dict[str, None] = {}
        for url in [*self.placeholders, *self.unverified]:
            if url not in seen:
                seen[url] = None
        return list(seen)

    @property
    def has_issues(self) -> bool:
        return bool(self.flagged)


def analyze_citations(
    response_text: str | None,
    evidence_urls: set[str] | None,
    *,
    evidence_available: bool | None = None,
) -> CitationReport:
    """Analyze the citations in ``response_text`` against ``evidence_urls``.

    ``evidence_available`` controls provenance checking: when there is no tool
    evidence (e.g. a pure LLM answer with no tools), URLs are only flagged if
    they have an obvious placeholder shape -- a legitimate reference like
    ``https://python.org`` is left alone. When evidence *is* available, any
    cited URL absent from it is additionally flagged as unverified.
    """
    report = CitationReport(cited=extract_urls(response_text))
    # Normalize defensively so a caller that passes raw URLs still matches.
    evidence = {normalize_url(u) for u in (evidence_urls or set()) if u}
    if evidence_available is None:
        evidence_available = bool(evidence)
    report.evidence_available = evidence_available

    for url in report.cited:
        if is_placeholder_url(url):
            report.placeholders.append(url)
        elif evidence_available and normalize_url(url) not in evidence:
            report.unverified.append(url)

    return report


def build_warning_markdown(report: CitationReport) -> str:
    """Build the visible warning block appended to a response with issues."""
    reasons: list[str] = []
    if report.placeholders:
        reasons.append(
            f"{len(report.placeholders)} cite an obvious placeholder identifier "
            f"(e.g. `abc123`, `XYZ`), which cannot be a real source"
        )
    if report.unverified:
        reasons.append(
            f"{len(report.unverified)} do not appear in any source retrieved "
            f"during this request"
        )
    reason_text = "; ".join(reasons) if reasons else "unverifiable"

    lines = [
        "",
        "---",
        "",
        "> [!WARNING]",
        "> **Unverified citations detected.** "
        f"{reason_text}. The following cited link(s) may be fabricated and could "
        "not be matched to retrieved evidence:",
        ">",
    ]

    placeholder_set = set(report.placeholders)
    for url in report.flagged:
        tag = " _(placeholder identifier)_" if url in placeholder_set else " _(not retrieved)_"
        lines.append(f"> - `{url}`{tag}")

    lines.extend(
        [
            ">",
            "> Any claim that rests on these citations should be treated as "
            "unverified until the source is independently confirmed.",
            "",
        ]
    )
    return "\n".join(lines)


def _marker_comment(report: CitationReport) -> str:
    return (
        f'{_UNVERIFIED_MARKER_PREFIX} count="{len(report.flagged)}" '
        f'placeholders="{len(report.placeholders)}" '
        f'unverified="{len(report.unverified)}" -->'
    )


def annotate_response_content(
    content: str | None,
    evidence_urls: set[str] | None,
    *,
    evidence_available: bool | None = None,
) -> tuple[str | None, CitationReport]:
    """Append a citation warning to ``content`` when citations are suspect.

    Returns ``(new_content, report)``. When there is nothing to flag,
    ``new_content`` is the original ``content`` unchanged (same object) so the
    guard is a strict no-op for clean responses.
    """
    report = analyze_citations(
        content, evidence_urls, evidence_available=evidence_available
    )
    if not report.has_issues:
        return content, report

    warning = build_warning_markdown(report)
    marker = _marker_comment(report)
    base = content or ""
    new_content = f"{base}{warning}\n{marker}"
    return new_content, report


def annotate_response(
    response: Any,
    messages: Iterable[Any] | None,
    *,
    trace_logger: Any = None,
) -> CitationReport | None:
    """Apply the citation guard to a ``CreateChatCompletionResponse``.

    Collects tool evidence from ``messages``, analyzes the response's first
    choice content, and -- if any citation looks fabricated -- appends the
    warning block in place and records a trace event. Returns the report (or
    ``None`` when the guard is disabled / the response is unusable).
    """
    if not get_citation_guard_enabled():
        return None

    try:
        choices = getattr(response, "choices", None)
        if not choices:
            return None
        message = getattr(choices[0], "message", None)
        if message is None:
            return None
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            return None

        evidence_urls = collect_evidence_urls(messages)
        new_content, report = annotate_response_content(content, evidence_urls)
        if not report.has_issues:
            return report

        message.content = new_content
        logger.warning(
            "citation guard flagged {} suspect citation(s) "
            "(placeholders={}, unverified={})",
            len(report.flagged),
            len(report.placeholders),
            len(report.unverified),
        )
        if trace_logger is not None:
            try:
                trace_logger.record(
                    "unverified_citations",
                    placeholders=report.placeholders,
                    unverified=report.unverified,
                    evidence_size=len(evidence_urls),
                    evidence_available=report.evidence_available,
                )
            except Exception:
                pass
        return report
    except Exception as exc:  # never let the guard break a response
        logger.debug(f"citation guard failed (ignored): {exc}")
        return None
