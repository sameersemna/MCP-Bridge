"""Tests for the citation provenance guard.

Regression coverage for a real failure: a generated report cited fabricated
URLs (``https://www.youtube.com/watch?v=abc123``,
``https://books.google.com/books?id=XYZ``,
``https://www.facebook.com/<page>/posts/123456789``) while asserting the
sources were real. The guard exists so a fabricated citation is *surfaced* to
the reader rather than silently presented as authoritative.
"""

from types import SimpleNamespace

import pytest

from mcp_bridge.openai_clients import citation_guard as cg


# ---------------------------------------------------------------------------
# URL extraction / normalization
# ---------------------------------------------------------------------------


def test_extract_urls_dedupes_and_preserves_order():
    text = "See https://a.com/x and https://b.com/y. Again https://a.com/x."
    assert cg.extract_urls(text) == ["https://a.com/x", "https://b.com/y"]


def test_extract_urls_strips_trailing_prose_punctuation():
    assert cg.extract_urls("Ref: https://a.com/x).") == ["https://a.com/x"]


def test_normalize_url_ignores_www_scheme_case_and_fragment():
    # Scheme, host case, leading `www.`, and fragment are all irrelevant to
    # which source a URL points at.
    assert cg.normalize_url("http://WWW.Example.com/Path/#frag") == "//example.com/Path"
    assert cg.normalize_url("https://example.com/Path") == "//example.com/Path"


# ---------------------------------------------------------------------------
# Placeholder detection -- the exact URLs from the real report
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://www.youtube.com/watch?v=def456",
        "https://www.youtube.com/watch?v=ghi789",
        "https://www.facebook.com/blal.bn.bdalghny.alsalmy/posts/123456789",
        "https://books.google.com/books?id=XYZ",
        "https://scholar.google.com/scholar?cluster=12345",
        "https://example.com/articles/foo",
        "https://foo.com/your-url",
    ],
)
def test_is_placeholder_url_flags_fabricated_shapes(url):
    assert cg.is_placeholder_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://shamela.ws/book/1075/292",
        "https://en.salafiyyah.mv/tag/abdullah-al-bukhari/",
        "https://openrouter.ai/apps",
        "https://scholar.google.com/citations?user=AbCdEf123",
    ],
)
def test_is_placeholder_url_allows_real_looking_urls(url):
    assert cg.is_placeholder_url(url) is False


# ---------------------------------------------------------------------------
# Evidence collection
# ---------------------------------------------------------------------------


def test_collect_evidence_urls_from_messages():
    messages = [
        {"role": "user", "content": "find something"},
        {"role": "tool", "content": "Result: https://real-source.example.org/page"},
        {"role": "tool", "content": "Another https://other.org/a"},
    ]
    evidence = cg.collect_evidence_urls(messages)
    assert cg.normalize_url("https://real-source.example.org/page") in evidence
    assert cg.normalize_url("https://other.org/a") in evidence


def test_update_evidence_from_text_handles_non_string():
    evidence: set[str] = set()
    cg.update_evidence_from_text(evidence, [{"url": "https://x.org/y"}])
    assert cg.normalize_url("https://x.org/y") in evidence


# ---------------------------------------------------------------------------
# Analysis + annotation
# ---------------------------------------------------------------------------


def test_analyze_flags_placeholders_even_with_no_evidence():
    report = cg.analyze_citations(
        "See https://www.youtube.com/watch?v=abc123 for details.",
        evidence_urls=set(),
        evidence_available=False,
    )
    assert report.placeholders == ["https://www.youtube.com/watch?v=abc123"]
    assert report.unverified == []
    assert report.has_issues is True


def test_analyze_does_not_flag_legit_url_when_no_evidence():
    report = cg.analyze_citations(
        "See https://python.org for details.",
        evidence_urls=set(),
        evidence_available=False,
    )
    assert report.has_issues is False


def test_analyze_flags_cited_but_unretrieved_url_as_unverified():
    report = cg.analyze_citations(
        "Per https://fake-news.org/article, the claim holds.",
        evidence_urls={"https://real-source.org/page"},
        evidence_available=True,
    )
    assert report.unverified == ["https://fake-news.org/article"]
    assert report.placeholders == []
    assert report.has_issues is True


def test_analyze_accepts_cited_url_present_in_evidence():
    report = cg.analyze_citations(
        "Per https://real-source.org/page, the claim holds.",
        evidence_urls={"https://real-source.org/page"},
        evidence_available=True,
    )
    assert report.has_issues is False


def test_analyze_provenance_survives_www_and_scheme_differences():
    report = cg.analyze_citations(
        "Per https://www.real-source.org/page.",
        evidence_urls={"http://real-source.org/page"},
        evidence_available=True,
    )
    assert report.has_issues is False


def test_annotate_response_content_appends_visible_warning():
    content = "Findings. Source: https://www.youtube.com/watch?v=abc123"
    new_content, report = cg.annotate_response_content(
        content, evidence_urls=set(), evidence_available=False
    )
    assert report.has_issues is True
    assert new_content.startswith(content)
    assert "Unverified citations detected" in new_content
    assert "https://www.youtube.com/watch?v=abc123" in new_content
    assert "mcp-bridge:unverified-citations" in new_content


def test_annotate_response_content_is_noop_when_clean():
    content = "Findings. Source: https://real-source.org/page"
    new_content, report = cg.annotate_response_content(
        content, evidence_urls={"https://real-source.org/page"}, evidence_available=True
    )
    assert report.has_issues is False
    assert new_content is content


# ---------------------------------------------------------------------------
# End-to-end: annotate a response object
# ---------------------------------------------------------------------------


def _make_response(content: str) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_annotate_response_flags_report_like_content():
    fabricated = (
        "**Conclusion**\n\nAll sources are publicly accessible.\n\n"
        "[1] https://www.youtube.com/watch?v=abc123\n"
        "[2] https://books.google.com/books?id=XYZ\n"
        "[3] https://scholar.google.com/scholar?cluster=12345\n"
    )
    response = _make_response(fabricated)
    messages = [
        {"role": "tool", "content": "Retrieved: https://en.salafiyyah.mv/tag/abdullah-al-bukhari/"},
    ]

    report = cg.annotate_response(response, messages)

    assert report is not None
    assert report.has_issues
    assert len(report.flagged) == 3
    assert "Unverified citations detected" in response.choices[0].message.content


def test_annotate_response_clean_report_untouched():
    content = "Per https://en.salafiyyah.mv/tag/abdullah-al-bukhari/, confirmed."
    response = _make_response(content)
    messages = [
        {"role": "tool", "content": "Retrieved: https://en.salafiyyah.mv/tag/abdullah-al-bukhari/"},
    ]

    cg.annotate_response(response, messages)

    assert response.choices[0].message.content == content


def test_annotate_response_disabled(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_CITATION_GUARD", "false")
    content = "See https://www.youtube.com/watch?v=abc123"
    response = _make_response(content)

    report = cg.annotate_response(response, [])

    assert report is None
    assert response.choices[0].message.content == content


def test_annotate_response_never_raises_on_bad_input():
    # A response with no choices must be a harmless no-op, not an exception.
    assert cg.annotate_response(SimpleNamespace(choices=[]), []) is None
