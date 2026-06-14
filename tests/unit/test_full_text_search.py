"""Unit tests for full-text content search.

Two layers are covered:

* ``SearchClient.full_text_search`` -- the OCS unified-search call: response
  mapping (snippet/path/resource_url), the "(files) " snippet-prefix strip,
  empty results, and OCS error propagation.
* ``nc_files_full_text_search`` (server tool) -- wiring: that it filters hits
  through ``EXCLUDED_TAGS`` and turns a 404 (provider not installed) into a
  friendly ``ToolError`` rather than leaking the raw HTTP error.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from nextcloud_mcp_server.client.search import SearchClient
from nextcloud_mcp_server.server.webdav import configure_webdav_tools

pytestmark = pytest.mark.unit


def _ocs(entries: list[dict], statuscode: int = 200) -> dict:
    return {"ocs": {"meta": {"statuscode": statuscode}, "data": {"entries": entries}}}


def _make_client(mocker) -> SearchClient:
    return SearchClient(mocker.AsyncMock(spec=httpx.AsyncClient), "alice")


# ── client layer ────────────────────────────────────────────────────────


async def test_full_text_search_maps_entries(mocker):
    """Each OCS entry maps to snippet (prefix stripped) / path / resource_url."""
    client = _make_client(mocker)
    resp = mocker.Mock()
    resp.json.return_value = _ocs(
        [
            {
                "title": "(files) quarterly revenue summary",
                "subline": "SKF/OneDrive/report.xlsx",
                "resourceUrl": "/apps/files/?dir=/SKF/OneDrive&scrollto=report.xlsx",
            }
        ]
    )
    client._make_request = AsyncMock(return_value=resp)

    results = await client.full_text_search("revenue", limit=5)

    assert results == [
        {
            "snippet": "quarterly revenue summary",
            "path": "SKF/OneDrive/report.xlsx",
            "resource_url": "/apps/files/?dir=/SKF/OneDrive&scrollto=report.xlsx",
        }
    ]
    # term + limit are forwarded as query params.
    kwargs = client._make_request.await_args.kwargs
    assert kwargs["params"] == {"term": "revenue", "limit": 5}


async def test_full_text_search_keeps_snippet_without_prefix(mocker):
    """A snippet that lacks the '(files) ' prefix is returned unchanged."""
    client = _make_client(mocker)
    resp = mocker.Mock()
    resp.json.return_value = _ocs(
        [{"title": "no prefix here", "subline": "a.txt", "resourceUrl": ""}]
    )
    client._make_request = AsyncMock(return_value=resp)

    results = await client.full_text_search("x")

    assert results[0]["snippet"] == "no prefix here"
    # limit omitted -> only term is sent.
    assert client._make_request.await_args.kwargs["params"] == {"term": "x"}


async def test_full_text_search_empty(mocker):
    client = _make_client(mocker)
    resp = mocker.Mock()
    resp.json.return_value = _ocs([])
    client._make_request = AsyncMock(return_value=resp)

    assert await client.full_text_search("nothing") == []


async def test_full_text_search_ocs_error_raises(mocker):
    client = _make_client(mocker)
    resp = mocker.Mock()
    resp.json.return_value = {
        "ocs": {"meta": {"statuscode": 996, "message": "boom"}, "data": {}}
    }
    client._make_request = AsyncMock(return_value=resp)

    with pytest.raises(RuntimeError, match="996.*boom"):
        await client.full_text_search("x")


# ── server tool layer ───────────────────────────────────────────────────


@pytest.fixture
def webdav_tools() -> dict:
    mcp = FastMCP(name="test-webdav-tools")
    configure_webdav_tools(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}


def _mock_ctx(client) -> SimpleNamespace:
    ctx = SimpleNamespace()
    ctx.request_context = SimpleNamespace(access_token=None)
    ctx._client = client
    return ctx


@pytest.fixture
def fake_client():
    client = SimpleNamespace()
    client.webdav = AsyncMock()
    client.search = AsyncMock()
    return client


@pytest.fixture
def patch_get_client(mocker):
    def _install(client):
        async def fake_get_client(ctx):
            return client

        mocker.patch(
            "nextcloud_mcp_server.server.webdav.get_client",
            side_effect=fake_get_client,
        )

    return _install


@pytest.fixture
def patch_excluded(mocker):
    def _install(excluded: set[str]):
        async def fake(*_, **__):
            return excluded

        mocker.patch(
            "nextcloud_mcp_server.server.webdav.get_excluded_file_paths",
            side_effect=fake,
        )

    return _install


async def test_tool_filters_excluded_paths(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    """Hits inside an excluded-tag folder must not leak through search."""
    patch_get_client(fake_client)
    patch_excluded({"Private"})
    fake_client.search.full_text_search = AsyncMock(
        return_value=[
            {"snippet": "secret", "path": "Private/secret.txt", "resource_url": ""},
            {"snippet": "ok", "path": "Public/notes.md", "resource_url": ""},
        ]
    )

    fn = webdav_tools["nc_files_full_text_search"].fn
    result = await fn(query="x", ctx=_mock_ctx(fake_client))

    paths = [r.path for r in result.results]
    assert paths == ["Public/notes.md"]
    assert result.total_found == 1
    assert result.query == "x"


async def test_tool_maps_404_to_tool_error(
    webdav_tools, fake_client, patch_get_client, patch_excluded
):
    """A missing fulltextsearch provider (404) becomes a friendly ToolError."""
    patch_get_client(fake_client)
    patch_excluded(set())
    request = httpx.Request("GET", "http://nc/ocs")
    response = httpx.Response(404, request=request)
    fake_client.search.full_text_search = AsyncMock(
        side_effect=httpx.HTTPStatusError("not found", request=request, response=response)
    )

    fn = webdav_tools["nc_files_full_text_search"].fn
    with pytest.raises(ToolError, match="files_fulltextsearch"):
        await fn(query="x", ctx=_mock_ctx(fake_client))
