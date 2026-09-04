"""Standalone MCP stdio server exposing a single ``read_pdf`` tool.

This is a small first-party MCP server (installed as the ``mcp-bridge-pdf-tool``
console script and invoked via ``uv run --no-project mcp-bridge-pdf-tool`` from
config.json, the same pattern already used for ``neo4j-mcp-server``) rather
than a third-party PDF-reading package. Third-party ``npx``/``uvx`` MCP
packages in this project have repeatedly turned out to be fragile (slow cold
installs, deprecated/abandoned packages, permission pitfalls on their cache
volumes); a tool this simple doesn't need that risk, and it ships in the same
image as the rest of the bridge with no extra runtime download.

The general PDF-recovery interceptor in ``chatCompletion.py`` already fixes
PDFs fetched incidentally by other tools. This tool is for the complementary
case: the model already suspects a link is a PDF and can call it directly
instead of wasting a round-trip through a generic fetch tool that would just
return unusable raw bytes.
"""

import io

import httpx
import pypdf
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pdf-reader")

DEFAULT_MAX_LENGTH = 20000
MAX_DOWNLOAD_BYTES = 25_000_000
FETCH_TIMEOUT_SECONDS = 30.0


async def _read_pdf_impl(url: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    if not url or not url.lower().startswith(("http://", "https://")):
        return "Error: url must be an http:// or https:// URL"

    try:
        raw = bytearray()
        async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT_SECONDS) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_DOWNLOAD_BYTES:
                        return f"Error: PDF exceeded the {MAX_DOWNLOAD_BYTES}-byte download limit"
    except httpx.HTTPError as e:
        return f"Error: could not fetch {url} ({e})"

    if not bytes(raw).startswith(b"%PDF-"):
        return f"Error: {url} does not look like a PDF (no %PDF- magic bytes found)"

    try:
        reader = pypdf.PdfReader(io.BytesIO(bytes(raw)))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages_text).strip()
    except Exception as e:
        return f"Error: failed to parse PDF from {url} ({type(e).__name__}: {e})"

    if not text:
        return f"Error: no extractable text layer found in {url} (it may be a scanned/image-only PDF)"

    if len(text) > max_length:
        text = text[:max_length].rstrip() + "…"

    return text


@mcp.tool()
async def read_pdf(url: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Fetch a PDF from a URL and return its extracted text.

    Use this instead of a generic web-fetch tool whenever a link is already
    known or suspected to point at a PDF -- generic fetch tools return raw,
    unusable bytes for PDF content, this returns the actual text.

    Args:
        url: Direct URL to the PDF file.
        max_length: Maximum characters of extracted text to return.
    """
    return await _read_pdf_impl(url, max_length)


def run() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run()
