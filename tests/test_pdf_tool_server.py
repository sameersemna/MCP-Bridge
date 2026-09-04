import asyncio

import httpx

from mcp_bridge import pdf_tool_server


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
    def __init__(self, chunks=None, raises=None, **kwargs):
        self._chunks = chunks or []
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def stream(self, method, url):
        if self._raises is not None:
            raise self._raises
        return _FakeStreamContextManager(_FakeStreamResponse(self._chunks))


def _patch_httpx_client(monkeypatch, *, chunks=None, raises=None):
    def factory(*args, **kwargs):
        return _FakeAsyncClient(chunks=chunks, raises=raises)

    monkeypatch.setattr(pdf_tool_server.httpx, "AsyncClient", factory)


def test_read_pdf_returns_extracted_text(monkeypatch):
    pdf_bytes = _build_minimal_pdf("Direct Tool Fetch")
    _patch_httpx_client(monkeypatch, chunks=[pdf_bytes])

    result = asyncio.run(pdf_tool_server._read_pdf_impl("https://example.com/report.pdf"))
    assert result == "Direct Tool Fetch"


def test_read_pdf_rejects_non_http_url():
    result = asyncio.run(pdf_tool_server._read_pdf_impl("ftp://example.com/report.pdf"))
    assert result.startswith("Error:")


def test_read_pdf_reports_fetch_failure(monkeypatch):
    _patch_httpx_client(monkeypatch, raises=httpx.ConnectError("boom"))

    result = asyncio.run(pdf_tool_server._read_pdf_impl("https://example.com/report.pdf"))
    assert result.startswith("Error:")


def test_read_pdf_reports_when_url_is_not_actually_a_pdf(monkeypatch):
    _patch_httpx_client(monkeypatch, chunks=[b"<html>not a pdf</html>"])

    result = asyncio.run(pdf_tool_server._read_pdf_impl("https://example.com/page.html"))
    assert "does not look like a PDF" in result


def test_read_pdf_truncates_to_max_length(monkeypatch):
    pdf_bytes = _build_minimal_pdf("A" * 100)
    _patch_httpx_client(monkeypatch, chunks=[pdf_bytes])

    result = asyncio.run(pdf_tool_server._read_pdf_impl("https://example.com/report.pdf", max_length=10))
    assert len(result) <= 11  # 10 chars + the truncation ellipsis
    assert result.startswith("A" * 10)
