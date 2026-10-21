"""Resource content keeps its MIME type and handles text and blobs (P3-01)."""

from __future__ import annotations

import base64

import mcp.types as types
from pydantic import AnyUrl

from mcp_bridge.mcp_server.server import _resource_contents


def test_custom_text_mime_is_preserved() -> None:
    resource = types.TextResourceContents(
        uri=AnyUrl("file:///data.json"),
        mimeType="application/json",
        text='{"a": 1}',
    )

    item = _resource_contents(resource)

    assert item.content == '{"a": 1}'
    assert item.mime_type == "application/json"


def test_blob_mime_is_preserved() -> None:
    resource = types.BlobResourceContents(
        uri=AnyUrl("file:///x.png"),
        mimeType="image/png",
        blob=base64.b64encode(b"\x89PNG").decode("ascii"),
    )

    item = _resource_contents(resource)

    assert item.content == b"\x89PNG"
    assert item.mime_type == "image/png"


def test_base64_string_blob_is_decoded() -> None:
    resource = types.BlobResourceContents.model_construct(
        uri=AnyUrl("file:///x.bin"),
        mimeType="application/octet-stream",
        blob=base64.b64encode(b"abc").decode("ascii"),
    )

    item = _resource_contents(resource)

    assert item.content == b"abc"
