"""Keenable search adapter. Intern's web-search pack supports only brave/tavily/exa/searxng, so external research
goes through this server adapter; results are saved as an EXTERNAL source in the room's workspace, where System 2
reads them under Drex's guard like any other file."""

from __future__ import annotations

import os
from typing import Any

import httpx

KEENABLE_URL = "https://api.keenable.ai/v1/search"


class NotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("KEENABLE_API_KEY"))


def _results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items", "documents", "hits"):
            val = payload.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
            if isinstance(val, dict):
                inner = _results(val)
                if inner:
                    return inner
    return []


async def search(query: str) -> dict[str, Any]:
    key = os.environ.get("KEENABLE_API_KEY")
    if not key:
        raise NotConfigured("Keenable not configured")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(KEENABLE_URL, headers={"X-API-Key": key, "Content-Type": "application/json"},
                              json={"query": query})
    if r.status_code >= 400:
        raise RuntimeError(f"Keenable search failed: HTTP {r.status_code}: {r.text[:300]}")
    payload = r.json()
    items = []
    for res in _results(payload)[:8]:
        items.append({
            "title": str(res.get("title") or res.get("name") or "Untitled"),
            "url": str(res.get("url") or res.get("link") or ""),
            "snippet": str(res.get("snippet") or res.get("description") or res.get("content") or res.get("text")
                           or "")[:1500],
        })
    return {"query": query, "results": items, "raw": payload}


def to_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# EXTERNAL research (Keenable): {result['query']}",
        "",
        "> EXTERNAL source. Lower priority than every course source; it never overrides course material.",
        "",
    ]
    for i, item in enumerate(result["results"], 1):
        lines += [f"## [{i}] {item['title']}", f"URL: {item['url']}", "", item["snippet"], ""]
    if not result["results"]:
        lines.append("(Keenable returned no results.)")
    return "\n".join(lines)
