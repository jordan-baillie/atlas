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

HOMER_PERSONA_PATH = Path("/root/.pi/agent/HOMER_PERSONA.md")


def _load_system_prompt() -> str:
    """Load the HomerBot persona as the pi subprocess system prompt.

    The persona is intentionally scoped here — it applies ONLY to Atlas
    dashboard chat sessions (HomerBot at /homerbot). Do NOT move this into
    /root/.pi/agent/AGENTS.md, which is loaded by every pi subprocess in the
    system and would leak the persona into team leads, workers, and the
    orchestrator.

    Falls back to the generic Claude Code prompt if the persona file is
    missing so the dashboard still works if someone deletes it.
    """
    if HOMER_PERSONA_PATH.exists():
        try:
            text = HOMER_PERSONA_PATH.read_text().strip()
            if text:
                return text
        except OSError:
            logger.warning(
                "Failed to read HomerBot persona at %s; falling back",
                HOMER_PERSONA_PATH,
            )
    return "You are Claude Code, Anthropic's official CLI for Claude."


_SYSTEM_PROMPT = _load_system_prompt()


# ── Attachment materialisation ───────────────────────────────────────────────

# Cap conversion size to keep prompts sane.  Excel sheets larger than this
# are truncated with a clear marker so Claude knows the data was clipped.
_MAX_ROWS_PER_SHEET = 500
_MAX_COLS_PER_SHEET = 60
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB raw upload cap

_SPREADSHEET_EXTS = {".xlsx", ".xlsm", ".xls", ".xlsb", ".ods"}
_TEXT_EXTS = {".csv", ".tsv", ".txt", ".md", ".json", ".log"}


def _safe_name(name: str) -> str:
    """Strip the user-supplied filename to a safe basename for temp prefixes."""
    import re as _re
    base = Path(name or "file").name
    base = _re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base[:80] or "file"


def _xlsx_to_markdown(xlsx_path: Path, original_name: str) -> str:
    """Convert an Excel workbook into a compact markdown document.

    Each sheet becomes a section.  Large sheets are truncated to
    ``_MAX_ROWS_PER_SHEET`` × ``_MAX_COLS_PER_SHEET`` with an explicit
    marker so Claude knows there is more data not shown.
    """
    import pandas as pd  # local import — keeps module-load time tight

    try:
        sheets = pd.read_excel(xlsx_path, sheet_name=None, engine=None)
    except Exception as exc:
        return f"# {original_name}\n\n*Could not parse Excel file: {exc}*\n"

    parts: list[str] = [f"# Excel file: {original_name}"]
    if not sheets:
        parts.append("\n*(workbook contains no sheets)*\n")
        return "\n".join(parts)

    parts.append(f"Sheets: {', '.join(sheets.keys())}\n")
    for sheet_name, df in sheets.items():
        total_rows, total_cols = df.shape
        truncated = False
        if total_cols > _MAX_COLS_PER_SHEET:
            df = df.iloc[:, :_MAX_COLS_PER_SHEET]
            truncated = True
        if total_rows > _MAX_ROWS_PER_SHEET:
            df = df.head(_MAX_ROWS_PER_SHEET)
            truncated = True

        parts.append(f"## Sheet: {sheet_name} ({total_rows} rows × {total_cols} cols)")
        if df.empty:
            parts.append("*(empty sheet)*\n")
            continue
        try:
            parts.append(df.to_markdown(index=False))
        except Exception:
            # to_markdown() needs `tabulate`; fall back to CSV format
            parts.append("```csv")
            parts.append(df.to_csv(index=False).rstrip())
            parts.append("```")
        if truncated:
            parts.append(
                f"\n*(truncated to first {_MAX_ROWS_PER_SHEET} rows × "
                f"{_MAX_COLS_PER_SHEET} cols — original was {total_rows} × {total_cols})*"
            )
        parts.append("")
    return "\n".join(parts)


def _materialise_attachments(attachments: list[dict]) -> list[Path]:
    """Decode chat attachments to temp files Pi can read with @file syntax.

    * Excel (xlsx/xls/...) → converted to markdown text file
    * CSV/TSV/TXT → written through unchanged (with .txt suffix so Pi treats
      it as text)
    * Anything else → written through with original extension; Claude may or
      may not be able to read it, but at least we don't silently drop it.
    """
    import base64 as b64mod
    import tempfile

    paths: list[Path] = []
    for idx, att in enumerate(attachments):
        raw_b64 = att.get("data", "")
        name = att.get("name", f"attachment_{idx}")
        if not raw_b64:
            continue
        try:
            raw_bytes = b64mod.b64decode(raw_b64)
        except Exception as exc:
            logger.warning("Could not decode attachment %s: %s", name, exc)
            continue
        if len(raw_bytes) > _MAX_ATTACHMENT_BYTES:
            logger.warning(
                "Attachment %s exceeds %d bytes; skipping",
                name, _MAX_ATTACHMENT_BYTES,
            )
            continue

        ext = Path(name).suffix.lower()
        safe = _safe_name(name)

        if ext in _SPREADSHEET_EXTS:
            # Write the raw workbook to a temp file, then convert to markdown.
            src = Path(tempfile.mktemp(suffix=ext, prefix=f"atlas_xlsx_src_{idx}_"))
            try:
                src.write_bytes(raw_bytes)
                md_text = _xlsx_to_markdown(src, name)
            finally:
                try:
                    src.unlink(missing_ok=True)
                except Exception:
                    pass
            out = Path(tempfile.mktemp(
                suffix=".md",
                prefix=f"atlas_xlsx_{idx}_{safe.rsplit('.', 1)[0]}_",
            ))
            out.write_text(md_text, encoding="utf-8")
            paths.append(out)
            continue

        if ext in _TEXT_EXTS or not ext:
            out = Path(tempfile.mktemp(
                suffix=ext or ".txt",
                prefix=f"atlas_doc_{idx}_{safe.rsplit('.', 1)[0]}_",
            ))
            try:
                # Decode as utf-8 with fallback so binary garbage doesn't kill us
                out.write_text(raw_bytes.decode("utf-8", errors="replace"), encoding="utf-8")
            except Exception as exc:
                logger.warning("Could not write text attachment %s: %s", name, exc)
                continue
            paths.append(out)
            continue

        # Unknown type — write raw and let Claude attempt to read it.
        out = Path(tempfile.mktemp(
            suffix=ext, prefix=f"atlas_doc_{idx}_{safe.rsplit('.', 1)[0]}_",
        ))
        out.write_bytes(raw_bytes)
        paths.append(out)

    return paths


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

    Parameters
    ----------
    use_teams : bool
        If True, load the multi-team orchestrator extension.
        If False (default), run plain Claude for fast direct responses.

    Usage::

        mgr = PiSessionManager(session_id="abc123")
        async for event in mgr.send_message("analyse the equity curve"):
            await websocket.send_json(event.to_dict())
    """

    def __init__(self, session_id: str, model: str = "claude-opus-4-8", use_teams: bool = False) -> None:
        self.session_id = session_id
        self.model = model
        self.use_teams = use_teams
        self.process: Optional[asyncio.subprocess.Process] = None
        self.pi_session_path: Path = SESSIONS_DIR / f"{session_id}.jsonl"
        self._running: bool = False
        self._current_response: str = ""
        # Registered WebSocket queues (one per connected client)
        self._subscribers: list[asyncio.Queue] = []

    # ── Public interface ─────────────────────────────────────────────────────

    async def send_message(
        self, content: str, images: list[dict] | None = None,
        attachments: list[dict] | None = None,
    ) -> AsyncGenerator[PiEvent, None]:
        """Spawn Pi with *content* as the prompt and yield streaming events.

        The generator yields every :class:`PiEvent` as it arrives from
        stdout.  A terminal ``PiEvent("done", {"full_text": ...})`` is always
        yielded last, even if Pi exits with an error.

        Parameters
        ----------
        content : str
            The user's text message.
        images : list[dict] | None
            Optional list of ``{"data": "<base64>", "mime": "image/png"}``
            dicts.  Each image is saved to a temp file and passed to Pi as
            an ``@/path/to/file`` argument.
        attachments : list[dict] | None
            Optional list of ``{"name": "file.xlsx", "data": "<base64>",
            "mime": "..."}`` dicts for spreadsheet/text documents.
            Excel files are converted to markdown server-side (Claude
            cannot read binary xlsx).  CSV/TSV/TXT files pass through.
        """
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # Write any attached images to temp files
        temp_image_paths: list[Path] = []
        if images:
            import base64 as b64mod
            import tempfile
            for idx, img in enumerate(images):
                raw = img.get("data", "")
                mime = img.get("mime", "image/png")
                ext = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                }.get(mime, ".png")
                try:
                    img_bytes = b64mod.b64decode(raw)
                    tmp = Path(tempfile.mktemp(suffix=ext, prefix=f"atlas_chat_img{idx}_"))
                    tmp.write_bytes(img_bytes)
                    temp_image_paths.append(tmp)
                except Exception as exc:
                    logger.warning("Failed to decode attached image %d: %s", idx, exc)

        # Materialise document attachments (xlsx, csv, tsv, txt)
        temp_doc_paths: list[Path] = []
        if attachments:
            temp_doc_paths = _materialise_attachments(attachments)

        cmd = self._build_cmd()
        # Attach images as @file references (Pi reads them as message attachments)
        for p in temp_image_paths:
            cmd.append(f"@{p}")
        for p in temp_doc_paths:
            cmd.append(f"@{p}")
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

            # Drain stderr in background to prevent pipe-buffer deadlock.
            # Pi writes warnings/debug to stderr; if the 64 KB pipe buffer
            # fills, the subprocess blocks on the next stderr write and stdout
            # stops producing data — making the WebSocket appear "stuck".
            async def _drain_stderr() -> None:
                try:
                    while True:
                        chunk = await self.process.stderr.read(65536)  # type: ignore[union-attr]
                        if not chunk:
                            break
                except Exception as e:
                    logger.debug("pi_session stderr drain: %s", e)

            stderr_task = asyncio.create_task(_drain_stderr())

            try:
                async with asyncio.timeout(600):  # 10-minute hard cap per message (Opus + big sessions need time)
                    # Read stdout in chunks instead of using readline().
                    # asyncio.StreamReader.readline() has a 64 KB default
                    # limit; Pi JSONL lines with thinking signatures +
                    # full message content easily exceed that, causing
                    # "Separator is not found, and chunk exceed the limit".
                    stdout_buf = ""
                    while True:
                        chunk = await self.process.stdout.read(262144)  # type: ignore[union-attr]
                        if not chunk:
                            # Process closed stdout — parse any remaining buffer
                            if stdout_buf.strip():
                                try:
                                    raw = json.loads(stdout_buf.strip())
                                    for evt in self._parse_jsonl_event(raw):
                                        await self._broadcast(evt)
                                        yield evt
                                except json.JSONDecodeError:
                                    pass
                            break
                        stdout_buf += chunk.decode("utf-8", errors="replace")
                        # Split on newlines — each complete line is one JSONL event
                        while "\n" in stdout_buf:
                            line, stdout_buf = stdout_buf.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                raw = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            for evt in self._parse_jsonl_event(raw):
                                await self._broadcast(evt)
                                yield evt
            except asyncio.TimeoutError:
                logger.warning("Pi subprocess timed out after 600 s")
                timeout_err = PiEvent("error", {"message": "Response timed out after 10 minutes. Try starting a new session — long history slows Opus down."})
                await self._broadcast(timeout_err)
                yield timeout_err
                if self.process:
                    self.process.terminate()
            finally:
                stderr_task.cancel()
                try:
                    await stderr_task
                except (asyncio.CancelledError, Exception):
                    pass

        except OSError as exc:
            err = PiEvent("error", {"message": f"Failed to start Pi: {exc}"})
            await self._broadcast(err)
            yield err

        finally:
            # Ensure the subprocess is fully terminated before we return
            if self.process:
                if self.process.returncode is None:
                    try:
                        self.process.terminate()
                        await asyncio.wait_for(self.process.wait(), timeout=5)
                    except (asyncio.TimeoutError, ProcessLookupError):
                        try:
                            self.process.kill()
                        except ProcessLookupError:
                            pass
                try:
                    await self.process.wait()
                except Exception as e:
                    logger.debug("pi_session process.wait() failed: %s", e)
            self._running = False

            # Clean up temp image files
            for p in temp_image_paths:
                try:
                    p.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug("pi_session temp file cleanup: %s", e)

            # Clean up temp doc files
            for p in temp_doc_paths:
                try:
                    p.unlink(missing_ok=True)
                except Exception as e:
                    logger.debug("pi_session temp doc cleanup: %s", e)

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
        """Build the pi CLI command list.

        Uses asyncio.create_subprocess_exec (async streaming) — a different
        execution model from atlas.kernel.pi_subprocess.call_pi (sync, captures
        output).  The --system-prompt flag below ensures Claude Max OAuth
        routing; this is the asyncio-equivalent of atlas.kernel.pi_subprocess.call_pi.
        """
        cmd = [
            PI_BIN,
            "--mode", "json",
            "-p",  # non-interactive / single-prompt mode
            "--model", self.model,
            "--session", str(self.pi_session_path),
            # Skip heavy startup discovery — keeps first-token latency low.
            # Skills are loaded via the multi-team extension when needed.
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--system-prompt", _SYSTEM_PROMPT,
        ]

        cmd.append("--no-extensions")

        # Only load multi-team orchestrator when explicitly requested.
        # Without it: plain Claude with tools (fast, ~16K token system prompt).
        # With it: full 10-agent orchestrator (~96K+ tokens, much slower).
        if self.use_teams and MULTI_TEAM_EXT.exists():
            cmd += ["-e", str(MULTI_TEAM_EXT)]

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
            tool_call_id = raw.get("toolCallId", "")
            # Rich handling for delegation/agent tools
            if tool in ("delegate", "spawn_worker", "subagent"):
                events.append(PiEvent("delegation_start", {
                    "tool": tool,
                    "target": args.get("target", args.get("name", "")),
                    "task_preview": str(args.get("prompt", args.get("task", args.get("objective", ""))))[:300],
                    "tool_call_id": tool_call_id,
                }))
            else:
                events.append(PiEvent("tool_start", {
                    "tool": tool,
                    "args": _summarize_args(args),
                    "tool_call_id": tool_call_id,
                }))

        elif evt_type == "tool_result":
            tool_call_id = raw.get("toolCallId", "")
            content = raw.get("content", [])
            text = ""
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text", "")
            details = raw.get("details", {})
            if isinstance(details, dict) and details.get("mode") == "delegation":
                results = details.get("results", [])
                events.append(PiEvent("delegation_end", {
                    "tool_call_id": tool_call_id,
                    "agents": [{"name": r.get("agent", ""), "team": r.get("team", ""),
                                "cost": r.get("cost", 0), "tokens": r.get("tokens", 0)}
                               for r in results],
                    "response_preview": text[:500] if text else "",
                }))
            else:
                events.append(PiEvent("tool_end", {
                    "tool_call_id": tool_call_id,
                    "result_preview": text[:1000] if text else "",
                }))

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
