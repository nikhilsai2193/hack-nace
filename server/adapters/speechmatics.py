"""Speechmatics batch v2: upload audio, poll the job, fetch the plain-text transcript. Typed input stays primary."""

from __future__ import annotations

import asyncio
import json
import os

import httpx

BASE = "https://asr.api.speechmatics.com/v2"


class NotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("SPEECHMATICS_API_KEY"))


async def transcribe(filename: str, data: bytes, content_type: str, language: str = "en") -> str:
    key = os.environ.get("SPEECHMATICS_API_KEY")
    if not key:
        raise NotConfigured("Speechmatics not configured")
    headers = {"Authorization": f"Bearer {key}"}
    config = {"type": "transcription", "transcription_config": {"language": language}}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"{BASE}/jobs", headers=headers,
                              files={"data_file": (filename, data, content_type or "application/octet-stream")},
                              data={"config": json.dumps(config)})
        if r.status_code >= 400:
            raise RuntimeError(f"Speechmatics job create failed: HTTP {r.status_code}: {r.text[:300]}")
        job_id = r.json()["id"]
        for _ in range(180):
            await asyncio.sleep(2)
            s = await client.get(f"{BASE}/jobs/{job_id}", headers=headers)
            status = s.json().get("job", {}).get("status")
            if status == "done":
                t = await client.get(f"{BASE}/jobs/{job_id}/transcript", headers=headers, params={"format": "txt"})
                t.raise_for_status()
                return t.text.strip()
            if status in ("rejected", "deleted", "expired"):
                raise RuntimeError(f"Speechmatics job {status}")
        raise RuntimeError("Speechmatics job timed out")


# ---------------- Text to speech (preview API: https://docs.speechmatics.com/text-to-speech/quickstart)

TTS_URL = "https://preview.tts.speechmatics.com/generate/{voice}"
# The four preview voices, one per persona.
PERSONA_VOICES = {"maya": "megan", "leo": "jack", "sam": "theo", "priya": "sarah"}
TTS_MAX_CHARS = 1500


async def synthesize(text: str, voice: str) -> bytes:
    """Returns a 16 kHz mono 16-bit WAV."""
    key = os.environ.get("SPEECHMATICS_API_KEY")
    if not key:
        raise NotConfigured("Speechmatics not configured")
    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(3):
            r = await client.post(TTS_URL.format(voice=voice), params={"output_format": "wav_16000"},
                                  headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                  json={"text": text[:TTS_MAX_CHARS]})
            if r.status_code == 503 and attempt < 2:  # documented: retry 503 with backoff
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Speechmatics TTS failed: HTTP {r.status_code}: {r.text[:300]}")
            return r.content
    raise RuntimeError("Speechmatics TTS unavailable (503)")
