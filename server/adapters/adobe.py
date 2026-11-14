"""Adobe PDF Services: HTML recap -> PDF via the REST API (token, asset upload, htmltopdf, poll, download)."""

from __future__ import annotations

import asyncio
import io
import os
import zipfile

import httpx

BASE = "https://pdf-services.adobe.io"


class NotConfigured(RuntimeError):
    pass


def credentials() -> tuple[str, str] | None:
    cid = os.environ.get("ADOBE_PDF_SERVICES_CLIENT_ID") or os.environ.get("ADOBE_CLIENT_ID")
    secret = os.environ.get("ADOBE_PDF_SERVICES_CLIENT_SECRET")
    return (cid, secret) if cid and secret else None


def configured() -> bool:
    return credentials() is not None


async def html_to_pdf(html: str) -> bytes:
    creds = credentials()
    if not creds:
        raise NotConfigured("Adobe not configured")
    cid, secret = creds
    async with httpx.AsyncClient(timeout=60) as client:
        tok = await client.post(f"{BASE}/token", data={"client_id": cid, "client_secret": secret})
        if tok.status_code >= 400:
            raise RuntimeError(f"Adobe token failed: HTTP {tok.status_code}: {tok.text[:300]}")
        token = tok.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "x-api-key": cid}

        asset = await client.post(f"{BASE}/assets", headers={**headers, "Content-Type": "application/json"},
                                  json={"mediaType": "application/zip"})
        if asset.status_code >= 400:
            raise RuntimeError(f"Adobe asset create failed: HTTP {asset.status_code}: {asset.text[:300]}")
        info = asset.json()

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("index.html", html)
        up = await client.put(info["uploadUri"], content=buf.getvalue(), headers={"Content-Type": "application/zip"})
        if up.status_code >= 400:
            raise RuntimeError(f"Adobe upload failed: HTTP {up.status_code}: {up.text[:300]}")

        op = await client.post(f"{BASE}/operation/htmltopdf", headers={**headers, "Content-Type": "application/json"},
                               json={"assetID": info["assetID"], "includeHeaderFooter": False,
                                     "pageLayout": {"pageWidth": 8.5, "pageHeight": 11}})
        if op.status_code >= 400 or "location" not in op.headers:
            raise RuntimeError(f"Adobe htmltopdf failed: HTTP {op.status_code}: {op.text[:300]}")
        location = op.headers["location"]

        for _ in range(60):
            await asyncio.sleep(2)
            poll = await client.get(location, headers=headers)
            body = poll.json()
            status = body.get("status")
            if status == "done":
                dl = await client.get(body["asset"]["downloadUri"])
                dl.raise_for_status()
                return dl.content
            if status == "failed":
                raise RuntimeError(f"Adobe job failed: {body.get('error')}")
        raise RuntimeError("Adobe job timed out")
