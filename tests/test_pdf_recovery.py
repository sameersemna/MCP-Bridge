import asyncio
import io

import httpx
import pypdf

from mcp_bridge.openai_clients import chatCompletion as chat_completion_module
from mcp_bridge.openai_clients.utils import _extract_url_argument


def _build_minimal_pdf(text: str) -> bytes:
    """Hand-build a small, well-formed single-page PDF with a real text layer."""
    content = f"BT /F1 24 Tf 72 120 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 200 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    return bytes(out)


def test_build_minimal_pdf_fixture_is_valid():
    pdf_bytes = _build_minimal_pdf("Hello PDF World")
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
    assert reader.pages[0].extract_text() == "Hello PDF World"


def test_extract_url_argument_reads_url_key():
    assert _extract_url_argument('{"url": "https://example.com/a.pdf", "max_length": 15000}') == (
        "https://example.com/a.pdf"
    )


def test_extract_url_argument_handles_missing_or_malformed_payload():
    assert _extract_url_argument("") == ""
    assert _extract_url_argument("not json") == ""
    assert _extract_url_argument('{"query": "no url here"}') == ""
    assert _extract_url_argument('["not", "a", "dict"]') == ""


def test_looks_like_mangled_pdf_text_detects_pdf_magic():
    assert chat_completion_module._looks_like_mangled_pdf_text("%PDF-1.3 %��� garbage")
    assert chat_completion_module._looks_like_mangled_pdf_text("   %PDF-1.7 leading whitespace")


def test_looks_like_mangled_pdf_text_rejects_normal_text():
    assert not chat_completion_module._looks_like_mangled_pdf_text("This is a normal search result summary.")
    assert not chat_completion_module._looks_like_mangled_pdf_text("")


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], status_error: Exception | None = None):
        self._chunks = chunks
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamContextManager:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient so tests never hit the real network."""

    instances: list["_FakeAsyncClient"] = []

    def __init__(self, chunks: list[bytes] | None = None, raises: Exception | None = None, **kwargs):
        self._chunks = chunks or []
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method: str, url: str):
        if self._raises is not None:
            raise self._raises
        return _FakeStreamContextManager(_FakeStreamResponse(self._chunks))


def _patch_httpx_client(monkeypatch, *, chunks=None, raises=None):
    def factory(*args, **kwargs):
        return _FakeAsyncClient(chunks=chunks, raises=raises)

    monkeypatch.setattr(chat_completion_module.httpx, "AsyncClient", factory)


def test_recover_pdf_text_from_url_succeeds_on_real_pdf_bytes(monkeypatch):
    pdf_bytes = _build_minimal_pdf("Recovered PDF Content")
    _patch_httpx_client(monkeypatch, chunks=[pdf_bytes[:20], pdf_bytes[20:]])

    result = asyncio.run(chat_completion_module._recover_pdf_text_from_url("https://example.com/doc.pdf"))
    assert result == "Recovered PDF Content"


def test_recover_pdf_text_from_url_returns_none_for_non_pdf_bytes(monkeypatch):
    _patch_httpx_client(monkeypatch, chunks=[b"<html>not a pdf</html>"])

    result = asyncio.run(chat_completion_module._recover_pdf_text_from_url("https://example.com/page.html"))
    assert result is None


def test_recover_pdf_text_from_url_returns_none_on_fetch_failure(monkeypatch):
    _patch_httpx_client(monkeypatch, raises=httpx.ConnectError("boom"))

    result = asyncio.run(chat_completion_module._recover_pdf_text_from_url("https://example.com/doc.pdf"))
    assert result is None


def test_recover_pdf_text_from_url_returns_none_for_non_http_url(monkeypatch):
    # Should short-circuit before ever constructing a client.
    def factory(*args, **kwargs):
        raise AssertionError("should not attempt a network fetch for a non-http(s) URL")

    monkeypatch.setattr(chat_completion_module.httpx, "AsyncClient", factory)

    result = asyncio.run(chat_completion_module._recover_pdf_text_from_url("file:///etc/passwd"))
    assert result is None


def test_recover_mangled_pdf_tool_content_replaces_mangled_text(monkeypatch):
    pdf_bytes = _build_minimal_pdf("Clean Extracted Text")
    _patch_httpx_client(monkeypatch, chunks=[pdf_bytes])

    tools_content = [{"type": "text", "text": "%PDF-1.3 %�� garbled binary junk"}]
    result = asyncio.run(
        chat_completion_module._recover_mangled_pdf_tool_content(
            "fetch_content", '{"url": "https://example.com/doc.pdf"}', tools_content
        )
    )
    assert result[0]["text"] == "Clean Extracted Text"


def test_recover_mangled_pdf_tool_content_leaves_normal_text_untouched(monkeypatch):
    def factory(*args, **kwargs):
        raise AssertionError("should not attempt recovery when content isn't PDF-mangled")

    monkeypatch.setattr(chat_completion_module.httpx, "AsyncClient", factory)

    tools_content = [{"type": "text", "text": "Found 10 search results: ..."}]
    result = asyncio.run(
        chat_completion_module._recover_mangled_pdf_tool_content(
            "search", '{"url": "https://example.com/doc.pdf"}', tools_content
        )
    )
    assert result[0]["text"] == "Found 10 search results: ..."


def test_recover_mangled_pdf_tool_content_leaves_content_untouched_without_url(monkeypatch):
    def factory(*args, **kwargs):
        raise AssertionError("should not attempt recovery when no url argument is available")

    monkeypatch.setattr(chat_completion_module.httpx, "AsyncClient", factory)

    tools_content = [{"type": "text", "text": "%PDF-1.3 garbled binary junk"}]
    result = asyncio.run(
        chat_completion_module._recover_mangled_pdf_tool_content(
            "fetch_content", '{"max_length": 15000}', tools_content
        )
    )
    assert result[0]["text"] == "%PDF-1.3 garbled binary junk"


def test_recover_mangled_pdf_tool_content_falls_back_when_refetch_is_not_actually_pdf(monkeypatch):
    _patch_httpx_client(monkeypatch, chunks=[b"<html>changed since the tool fetched it</html>"])

    original_text = "%PDF-1.3 garbled binary junk"
    tools_content = [{"type": "text", "text": original_text}]
    result = asyncio.run(
        chat_completion_module._recover_mangled_pdf_tool_content(
            "fetch_content", '{"url": "https://example.com/doc.pdf"}', tools_content
        )
    )
    assert result[0]["text"] == original_text
