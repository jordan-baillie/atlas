"""PiSessionManager — headless Pi subprocess for dashboard chat.

Spawns ``pi --mode json -p`` with the multi-team extension, parses the JSONL
output stream, and emits structured :class:`PiEvent` objects that the
WebSocket endpoint in chat_server.py forwards to browser clients.

Design decisions
----------------
* Each *chat session* maps to its own Pi session file on disk so that
  ``--continue`` can resume a conversation after a server restart or browser
  disconnect.
* The manager is **stateless between messages** — a new subprocess is spawned
  for every ``send_message()`` call (Pi non-interactive mode).  The Pi session
  file on disk carries the conversation history.
* Events are yielded as an async generator *and* broadcast to any
  :class:`asyncio.Queue` subscribers (for clients that reconnect mid-stream).
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import AsyncGenerator, Optional

logger = logging.getLogger("pi_session")

PROJECT_ROOT = Path("/root/atlas")
PI_BIN = "pi"  # Assumes pi is in PATH
MULTI_TEAM_EXT = Path("/root/.pi/extensions/multi-team/index.ts")
SESSIONS_DIR = PROJECT_ROOT / "data" / "chat" / "sessions"


# ── Event model ──────────────────────────────────────────────────────────────

class PiEvent:
    """One structured event parsed from Pi's JSONL output stream."""

    __slots__ = ("type", "data")

    def __init__(self, event_type: str, data: Optional[dict] = None) -> None:
        self.type: str = event_type
        self.data: dict = data or {}

    def to_dict(self) -> dict:
        return {"type": self.type, **self.data}

    def __repr__(self) -> str:  # pragma: no cover
        return f"PiEvent({self.type!r}, {self.data!r})"


# ── Manager ──────────────────────────────────────────────────────────────────

class PiSessionManager:
    """Manages one headless Pi subprocess per chat session.

    Usage::

        mgr = PiSessionManager(session_id="abc123")
        async for event in mgr.send_message("analyse the equity curve"):
            await websocket.send_json(event.to_dict())
    """

    def __init__(self, session_id: str, model: str = "claude-sonnet-4-6") -> None:
        self.session_id = session_id
        self.model = model
        self.process: Optional[asyncio.subprocess.Process] = None
        self.pi_session_path: Path = SESSIONS_DIR / f"{session_id}.jsonl"
        self._running: bool = False
        self._current_response: str = ""
        # Registered WebSocket queues (one per connected client)
        self._subscribers: list[asyncio.Queue] = []

    # ── Public interface ─────────────────────────────────────────────────────

    async def send_message(self, content: str) -> AsyncGenerator[PiEvent, None]:
        """Spawn Pi with *content* as the prompt and yield streaming events.

        The generator yields every :class:`PiEvent` as it arrives from
        stdout.  A terminal ``PiEvent("done", {"full_text": ...})`` is always
        yielded last, even if Pi exits with an error.
        """
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        cmd = self._build_cmd()
        # Append the user message as the final positional argument
        cmd.append(content)

        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT)

        logger.info("Spawning Pi subprocess: %s", " ".join(cmd[:6]) + " …")

        self._running = True
        self._current_response = ""

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                env=env,
            )

            async for line in self.process.stdout:  # type: ignore[union-attr]
                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded:
                    continue
                try:
                    raw = json.loads(decoded)
                except json.JSONDecodeError:
                    continue
                for evt in self._parse_jsonl_event(raw):
                    await self._broadcast(evt)
                    yield evt

        except OSError as exc:
            err = PiEvent("error", {"message": f"Failed to start Pi: {exc}"})
            await self._broadcast(err)
            yield err

        finally:
            if self.process:
                try:
                    await self.process.wait()
                except Exception:
                    pass
            self._running = False

            done = PiEvent("done", {"full_text": self._current_response})
            await self._broadcast(done)
            yield done

    async def cancel(self) -> None:
        """Terminate the running Pi process (SIGTERM)."""
        if self.process and self._running:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            finally:
                self._running = False

    # ── Subscriber fan-out (for reconnected WS clients) ──────────────────────

    def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber queue.  Returns the queue."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _build_cmd(self) -> list[str]:
        """Build the pi CLI command list."""
        cmd = [
            PI_BIN,
            "--mode", "json",
            "-p",  # non-interactive / single-prompt mode
        ]

        # Only add extension if it exists on disk
        if MULTI_TEAM_EXT.exists():
            cmd += ["-e", str(MULTI_TEAM_EXT)]

        cmd += [
            "--session", str(self.pi_session_path),
            "--model", self.model,
            "--no-extensions",  # don't auto-discover other extensions
        ]

        # Resume existing session so the conversation history is preserved
        if self.pi_session_path.exists():
            cmd.append("--continue")

        return cmd

    async def _broadcast(self, evt: PiEvent) -> None:
        """Put *evt* in every subscriber queue (non-blocking)."""
        for q in list(self._subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue full — dropping event %s", evt.type)

    def _parse_jsonl_event(self, raw: dict) -> list[PiEvent]:
        """Translate a Pi JSONL line into zero or more :class:`PiEvent` objects."""
        events: list[PiEvent] = []
        evt_type = raw.get("type", "")

        if evt_type == "message_update":
            ae = raw.get("assistantMessageEvent", {})
            ae_type = ae.get("type", "")

            if ae_type == "text_delta":
                delta = ae.get("delta", "")
                self._current_response += delta
                events.append(PiEvent("text_delta", {"delta": delta}))

            elif ae_type == "text_start":
                events.append(PiEvent("text_start", {}))

            elif ae_type == "text_end":
                events.append(PiEvent("text_end", {"full_text": self._current_response}))

            elif ae_type == "thinking_start":
                events.append(PiEvent("thinking_start", {}))

            elif ae_type == "thinking_delta":
                events.append(PiEvent("thinking_delta", {"delta": ae.get("delta", "")}))

            elif ae_type == "thinking_end":
                events.append(PiEvent("thinking_end", {}))

        elif evt_type == "tool_call":
            tool = raw.get("toolName") or raw.get("tool", "")
            args = raw.get("input", {})
            events.append(PiEvent("tool_start", {"tool": tool, "args": _summarize_args(args)}))

        elif evt_type == "tool_result":
            events.append(PiEvent("tool_end", {}))

        elif evt_type == "turn_end":
            msg = raw.get("message", {})
            usage = msg.get("usage", {})
            cost_info = usage.get("cost", {})
            events.append(
                PiEvent(
                    "turn_end",
                    {
                        "tokens": usage.get("totalTokens", 0),
                        "cost": cost_info.get("total", 0) if isinstance(cost_info, dict) else 0,
                    },
                )
            )

        elif evt_type == "error":
            message = raw.get("message") or raw.get("error") or "Unknown Pi error"
            events.append(PiEvent("error", {"message": str(message)}))

        return events


# ── Helpers ──────────────────────────────────────────────────────────────────

def _summarize_args(args: dict) -> dict:
    """Truncate long string values so they're safe to send to the browser."""
    result: dict = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            result[k] = v[:200] + "…"
        else:
            result[k] = v
    return result
