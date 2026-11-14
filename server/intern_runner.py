"""One long-lived `intern rpc` process per room: JSON-RPC 2.0 over stdio, one JSON object per line.

This module only speaks Intern's protocol. It never produces a model reply itself and has no fallback path:
when Intern is missing, refuses to start, or exits, the caller gets Intern's own stderr.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

SUPPORTED_PROTOCOL_VERSION = 2
STARTUP_TIMEOUT_S = 60.0
RPC_TIMEOUT_S = 30.0
SESSION_BUSY = -32000
SESSION_IDLE = -32001
CONFIG_ERROR = -32002


class InternNotFound(RuntimeError):
    pass


class InternStartupError(RuntimeError):
    def __init__(self, message: str, stderr: str = "", exit_code: int | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


class RpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def find_intern() -> str | None:
    found = shutil.which("intern")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/intern", Path("/opt/render/.local/bin/intern")):
        if candidate.exists():
            return str(candidate)
    return None


def intern_version(exe: str) -> str | None:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip() or None
    except Exception:
        return None


def intern_models(exe: str, env: dict[str, str]) -> tuple[dict[str, Any] | None, str]:
    """`intern models --json` with the room's environment. Returns (parsed, stderr)."""
    try:
        out = subprocess.run([exe, "models", "--json"], capture_output=True, text=True, timeout=60, env=env)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    try:
        return json.loads(out.stdout), out.stderr.strip()
    except json.JSONDecodeError:
        return None, (out.stderr or out.stdout).strip()


def intern_python(exe: str) -> str | None:
    """The interpreter of the `intern` executable (from its shebang), used to detect optional extras."""
    try:
        with open(os.path.realpath(exe), "rb") as f:
            first = f.readline().decode(errors="ignore").strip()
        if first.startswith("#!"):
            interp = first[2:].strip().split()[0]
            if os.path.exists(interp):
                return interp
    except OSError:
        pass
    sibling = Path(os.path.realpath(exe)).parent / "python"
    return str(sibling) if sibling.exists() else None


def has_module(python: str | None, module: str) -> bool:
    if not python:
        return False
    try:
        r = subprocess.run([python, "-c", f"import {module}"], capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


class InternProcess:
    def __init__(
        self,
        exe: str,
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        on_event: Callable[[dict[str, Any]], None],
        on_malformed: Callable[[str], None],
        on_exit: Callable[[int | None, str], None],
    ) -> None:
        self.exe = exe
        self.args = args
        self.cwd = cwd
        self.env = env
        self.on_event = on_event
        self.on_malformed = on_malformed
        self.on_exit = on_exit
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=200)
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._exited = asyncio.Event()
        self.exit_code: int | None = None
        self._write_lock = asyncio.Lock()

    @property
    def argv(self) -> list[str]:
        return [self.exe, *self.args]

    @property
    def alive(self) -> bool:
        return self.proc is not None and not self._exited.is_set()

    def stderr_text(self, lines: int = 40) -> str:
        return "\n".join(list(self.stderr_tail)[-lines:])

    async def start(self) -> dict[str, Any]:
        """Spawn and confirm with get_state. Intern sends no 'ready' message, so get_state answering IS ready."""
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=str(self.cwd),
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
        )
        self._tasks = [
            asyncio.create_task(self._read_stdout()),
            asyncio.create_task(self._read_stderr()),
            asyncio.create_task(self._watch_exit()),
        ]
        state_task = asyncio.create_task(self.request("get_state", {}, timeout=STARTUP_TIMEOUT_S))
        exit_task = asyncio.create_task(self._exited.wait())
        done, _ = await asyncio.wait({state_task, exit_task}, return_when=asyncio.FIRST_COMPLETED)
        if state_task in done and not state_task.cancelled() and state_task.exception() is None:
            exit_task.cancel()
            return state_task.result()
        state_task.cancel()
        exit_task.cancel()
        # Give stderr a moment to drain so the message is complete.
        await asyncio.sleep(0.3)
        if self._exited.is_set():
            raise InternStartupError(
                f"Intern exited during startup (exit code {self.exit_code})",
                stderr=self.stderr_text(),
                exit_code=self.exit_code,
            )
        err = state_task.exception() if state_task.done() and not state_task.cancelled() else None
        await self.stop()
        raise InternStartupError(f"Intern did not answer get_state: {err or 'timeout'}", stderr=self.stderr_text())

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = RPC_TIMEOUT_S) -> Any:
        if not self.proc or not self.proc.stdin or self._exited.is_set():
            raise RpcError(-1, "Intern process is not running")
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        line = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}) + "\n"
        async with self._write_lock:
            self.proc.stdin.write(line.encode())
            await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            try:
                raw = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as e:
                self.on_malformed(f"<line too long: {e}>")
                continue
            if not raw:
                break
            text = raw.decode(errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                self.on_malformed(text)
                continue
            if not isinstance(msg, dict):
                self.on_malformed(text)
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                fut = self._pending.get(msg["id"])
                if fut and not fut.done():
                    if "error" in msg and msg["error"]:
                        err = msg["error"]
                        fut.set_exception(RpcError(int(err.get("code", -1)), str(err.get("message", ""))))
                    else:
                        fut.set_result(msg.get("result"))
                continue
            if msg.get("method") == "event" and isinstance(msg.get("params"), dict):
                try:
                    self.on_event(msg["params"])
                except Exception as e:  # noqa: BLE001 — never let a UI bug kill the reader
                    self.on_malformed(f"<event handler failed: {type(e).__name__}: {e}> {text[:500]}")
                continue
            self.on_malformed(text)

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            raw = await self.proc.stderr.readline()
            if not raw:
                break
            self.stderr_tail.append(raw.decode(errors="replace").rstrip())

    async def _watch_exit(self) -> None:
        assert self.proc
        code = await self.proc.wait()
        # Let the readers drain.
        await asyncio.sleep(0.2)
        self.exit_code = code
        self._exited.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RpcError(-1, f"Intern process exited (code {code})"))
        self.on_exit(code, self.stderr_text())

    async def stop(self) -> None:
        if not self.proc or self._exited.is_set():
            return
        with contextlib.suppress(ProcessLookupError):
            self.proc.terminate()  # SIGTERM: Intern aborts the run and exits 143
        try:
            await asyncio.wait_for(self._exited.wait(), 8)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
