"""Nextcloud OCS unified-search client for full-text content search."""

import logging
from typing import Any

from .base import BaseNextcloudClient, retry_on_429

logger = logging.getLogger(__name__)

# Unified-search provider id registered by the ``files_fulltextsearch`` app. Its
# entries carry a content snippet (``title``) and the file path (``subline``).
_FULLTEXT_PROVIDER = "fulltextsearch"

# Snippets arrive prefixed with the matched provider's resource type, e.g.
# "(files) ...". Strip it so callers get the bare excerpt.
_SNIPPET_PREFIX = "(files) "


class SearchClient(BaseNextcloudClient):
    """Client for Nextcloud's OCS unified-search API (full-text content search)."""

    app_name = "search"

    @retry_on_429
    async def full_text_search(
        self, query: str, limit: int | None = None, exact_phrase: bool = False
    ) -> list[dict[str, Any]]:
        """Search file *contents* via the ``fulltextsearch`` search provider.

        Unlike WebDAV SEARCH, which matches only file metadata (name, MIME
        type), this matches indexed document text — including text extracted
        from PDFs and Office documents. Requires the ``files_fulltextsearch``
        app, backed by a search platform such as Elasticsearch, to be enabled
        on the server.

        Args:
            query: Free-text query to match against indexed file contents.
            limit: Maximum number of results to return.
            exact_phrase: When True, match the query as a contiguous phrase
                (the words in that order) rather than the individual words
                independently. Implemented with the provider's quoted-phrase
                syntax; any double quotes already in ``query`` are stripped.

        Returns:
            A list of ``{"snippet", "path", "resource_url"}`` dicts, one per hit.

        Raises:
            HTTPStatusError: If the request fails (e.g. 404 when the
                fulltextsearch provider is not installed).
            RuntimeError: If the OCS envelope reports a non-success status.
        """
        term = f'"{query.replace(chr(34), "")}"' if exact_phrase else query
        params: dict[str, Any] = {"term": term}
        if limit is not None:
            params["limit"] = limit

        response = await self._make_request(
            "GET",
            f"/ocs/v2.php/search/providers/{_FULLTEXT_PROVIDER}/search",
            params=params,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        data = response.json()

        meta = data["ocs"]["meta"]
        status = meta["statuscode"]
        # OCS v2 uses HTTP-style codes (200); v1 used 100. Accept both.
        if status not in (100, 200):
            message = meta.get("message", "Unknown error")
            raise RuntimeError(f"OCS API error (code {status}): {message}")

        entries = data["ocs"]["data"].get("entries", [])
        results: list[dict[str, Any]] = []
        for entry in entries:
            snippet = entry.get("title", "")
            if snippet.startswith(_SNIPPET_PREFIX):
                snippet = snippet[len(_SNIPPET_PREFIX) :]
            results.append(
                {
                    "snippet": snippet,
                    # ``subline`` is the file path relative to the user's files
                    # root — feeds directly into WebDAV read/list tools.
                    "path": entry.get("subline", ""),
                    "resource_url": entry.get("resourceUrl", ""),
                }
            )
        return results
