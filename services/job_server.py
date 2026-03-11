#!/usr/bin/env python3
"""Atlas Job Server — dispatch and manage Pi agent jobs via tmux.

The video's "listen" + "direct" equivalent. Manages the lifecycle of
agent jobs: create → queue → run (in tmux) → monitor → complete → notify.

Each job runs as a Pi agent in its own tmux session, isolated from the
main bot process. Jobs communicate back via completion files.

Usage:
    from services.job_server import JobManager
    mgr = JobManager()
    job = mgr.create_job("Run health check", skill="atlas-healthz")
    mgr.start_job(job["id"])
    mgr.get_job(job["id"])  # check status
    mgr.kill_job(job["id"])
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("atlas.job_server")

# ─── Configuration ───────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = PROJECT_ROOT / "jobs"
SPECS_DIR = PROJECT_ROOT / "specs"
LOGS_DIR = Path("/tmp/atlas-jobs")
DRIVE_SH = Path.home() / ".pi" / "agent" / "skills" / "drive" / "drive.sh"

# Skills that can be referenced by short name
SKILL_ALIASES = {
    "healthz": str(PROJECT_ROOT / "pi-package/atlas-ops/skills/atlas-healthz/atlas-healthz"),
    "health": str(PROJECT_ROOT / "pi-package/atlas-ops/skills/atlas-healthz/atlas-healthz"),
    "research": str(PROJECT_ROOT / "pi-package/atlas-ops/skills/atlas-research"),
    "research-loop": str(PROJECT_ROOT / "pi-package/atlas-ops/skills/atlas-research-loop"),
    "reoptimize": str(PROJECT_ROOT / "pi-package/atlas-ops/skills/atlas-reoptimize"),
    "daily": str(PROJECT_ROOT / "pi-package/atlas-ops/skills/atlas-daily"),
}

MAX_CONCURRENT_JOBS = 3
MAX_JOB_HISTORY = 50
JOB_TIMEOUT_S = 3600  # 1 hour default


# ─── Job Manager ─────────────────────────────────────────────────────────────

class JobManager:
    """Manages Pi agent jobs dispatched via tmux sessions."""

    def __init__(self):
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        SPECS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Job CRUD ─────────────────────────────────────────────────────

    def create_job(
        self,
        prompt: str,
        skill: Optional[str] = None,
        spec: Optional[str] = None,
        timeout: int = JOB_TIMEOUT_S,
    ) -> dict:
        """Create a new job (does not start it yet).

        Args:
            prompt: The task prompt for the Pi agent.
            skill: Optional skill name or path. Supports aliases (e.g. "healthz").
            spec: Optional spec file name (loaded from specs/ directory).
            timeout: Max runtime in seconds.

        Returns:
            Job dict with id, status, prompt, etc.
        """
        job_id = self._generate_id()
        now = datetime.now(timezone.utc)

        # Resolve spec file if provided
        if spec:
            spec_path = SPECS_DIR / spec
            if not spec_path.suffix:
                spec_path = spec_path.with_suffix(".md")
            if spec_path.exists():
                spec_content = spec_path.read_text()
                prompt = f"{spec_content}\n\n---\n\nAdditional instructions: {prompt}" if prompt else spec_content
            else:
                raise ValueError(f"Spec not found: {spec_path.name} (looked in {SPECS_DIR})")

        # Resolve skill alias
        skill_path = None
        if skill:
            skill_path = SKILL_ALIASES.get(skill, skill)
            # Validate path exists
            if not Path(skill_path).exists():
                raise ValueError(
                    f"Skill not found: {skill}. "
                    f"Available: {', '.join(sorted(SKILL_ALIASES.keys()))}"
                )

        job = {
            "id": job_id,
            "status": "queued",
            "prompt": prompt,
            "skill": skill_path,
            "skill_name": skill,
            "spec": spec,
            "timeout": timeout,
            "tmux_session": f"job-{job_id}",
            "pid": None,
            "log_file": str(LOGS_DIR / f"{job_id}.log"),
            "created_at": now.isoformat(),
            "started_at": None,
            "completed_at": None,
            "exit_code": None,
            "result_summary": None,
        }

        self._save_job(job)
        logger.info("Job created: %s", job_id)
        return job

    def start_job(self, job_id: str) -> dict:
        """Start a queued job in a tmux session.

        Returns updated job dict.
        Raises ValueError if job not found or not startable.
        """
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        if job["status"] not in ("queued", "failed"):
            raise ValueError(f"Job {job_id} is {job['status']}, cannot start")

        # Check concurrency limit
        running = self.list_jobs(status="running")
        if len(running) >= MAX_CONCURRENT_JOBS:
            raise ValueError(
                f"Max concurrent jobs ({MAX_CONCURRENT_JOBS}) reached. "
                f"Kill a running job first."
            )

        # Write prompt to a temp file (avoids shell escaping nightmares)
        prompt_file = LOGS_DIR / f"{job_id}.prompt"
        prompt_file.write_text(job["prompt"])

        # Write a launcher script that reads the prompt from the file
        launcher = LOGS_DIR / f"{job_id}.sh"
        log_file = job["log_file"]

        pi_cmd_parts = [
            "pi", "--print", "--no-session",
            "--model", "claude-sonnet-4-6",
        ]
        if job.get("skill"):
            pi_cmd_parts.extend(["--skill", job["skill"]])

        # The launcher reads the prompt from file, runs pi, captures exit code.
        # Pi's --print mode writes to stdout. We redirect to the log file
        # and use script(1) to capture output from the pseudo-terminal.
        launcher.write_text(
            f"#!/bin/bash\n"
            f"cd {PROJECT_ROOT}\n"
            f"PROMPT=$(cat {prompt_file})\n"
            f"{' '.join(pi_cmd_parts)} \"$PROMPT\" > {log_file} 2>&1\n"
            f"EXIT_CODE=$?\n"
            f"echo \"__JOB_EXIT_CODE__=$EXIT_CODE\" >> {log_file}\n"
            f"exit $EXIT_CODE\n"
        )
        launcher.chmod(0o755)

        # Launch in tmux via drive.
        # "bash launcher && exit" ensures tmux session dies when done,
        # so _refresh_running_job detects completion via session death.
        session_name = job["tmux_session"]
        drive_name = session_name.replace("job-", "")
        try:
            subprocess.run(
                [str(DRIVE_SH), "new", drive_name,
                 f"bash {launcher}; exit"],
                capture_output=True, text=True, timeout=10,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise ValueError(f"Failed to start tmux session: {e.stderr}")

        # Update job
        job["status"] = "running"
        job["started_at"] = datetime.now(timezone.utc).isoformat()
        self._save_job(job)

        logger.info("Job started: %s (session: %s)", job_id, session_name)
        return job

    def get_job(self, job_id: str) -> Optional[dict]:
        """Get job by ID. Returns None if not found."""
        job_path = JOBS_DIR / f"{job_id}.json"
        if not job_path.exists():
            return None
        try:
            with open(job_path) as f:
                job = json.load(f)
            # Auto-update status if running
            if job["status"] == "running":
                job = self._refresh_running_job(job)
            return job
        except Exception as e:
            logger.error("Failed to read job %s: %s", job_id, e)
            return None

    def list_jobs(
        self, status: Optional[str] = None, limit: int = 20
    ) -> list[dict]:
        """List jobs, optionally filtered by status.

        Returns most recent first.
        """
        jobs = []
        for p in sorted(JOBS_DIR.glob("*.json"), reverse=True):
            if len(jobs) >= limit:
                break
            try:
                with open(p) as f:
                    job = json.load(f)
                if status and job.get("status") != status:
                    continue
                # Refresh running jobs
                if job["status"] == "running":
                    job = self._refresh_running_job(job)
                jobs.append(job)
            except Exception:
                continue
        return jobs

    def kill_job(self, job_id: str) -> dict:
        """Kill a running job.

        Returns updated job dict.
        """
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        if job["status"] != "running":
            raise ValueError(f"Job {job_id} is {job['status']}, not running")

        # Kill tmux session via drive
        session_name = job["tmux_session"].replace("job-", "")
        try:
            subprocess.run(
                [str(DRIVE_SH), "kill", session_name],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            logger.warning("Failed to kill tmux session: %s", e)

        job["status"] = "killed"
        job["completed_at"] = datetime.now(timezone.utc).isoformat()
        job["exit_code"] = -9
        self._save_job(job)

        logger.info("Job killed: %s", job_id)
        return job

    def get_logs(self, job_id: str, lines: int = 30) -> str:
        """Get last N lines of a job's log output."""
        job = self.get_job(job_id)
        if not job:
            return f"Job {job_id} not found."

        log_path = Path(job["log_file"])
        if not log_path.exists():
            # Try reading from tmux session directly
            session_name = job["tmux_session"].replace("job-", "")
            try:
                result = subprocess.run(
                    [str(DRIVE_SH), "read", session_name, str(lines)],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
            return "No logs available yet."

        try:
            all_lines = log_path.read_text(errors="replace").splitlines()
            tail = all_lines[-lines:]
            return "\n".join(tail)
        except Exception as e:
            return f"Error reading logs: {e}"

    # ── Spec management ──────────────────────────────────────────────

    def list_specs(self) -> list[dict]:
        """List available spec files."""
        specs = []
        for p in sorted(SPECS_DIR.glob("*.md")):
            # Read first line for title
            try:
                first_line = p.read_text().split("\n")[0].strip().lstrip("#").strip()
            except Exception:
                first_line = p.stem
            specs.append({
                "name": p.stem,
                "title": first_line,
                "path": str(p),
                "size": p.stat().st_size,
            })
        return specs

    # ── Monitoring ───────────────────────────────────────────────────

    def check_completed_jobs(self) -> list[dict]:
        """Check all running jobs for completion.

        Returns list of jobs that just completed.
        Called periodically by the bot's monitoring loop.
        """
        completed = []
        for job in self.list_jobs(status="running", limit=MAX_CONCURRENT_JOBS + 5):
            updated = self._refresh_running_job(job)
            if updated["status"] in ("done", "failed", "timeout"):
                completed.append(updated)
        return completed

    def cleanup_old_jobs(self, keep: int = MAX_JOB_HISTORY):
        """Remove old job files and logs beyond the keep limit."""
        all_jobs = sorted(JOBS_DIR.glob("*.json"), reverse=True)
        for p in all_jobs[keep:]:
            job_id = p.stem
            # Remove job file
            p.unlink(missing_ok=True)
            # Remove log and prompt files
            (LOGS_DIR / f"{job_id}.log").unlink(missing_ok=True)
            (LOGS_DIR / f"{job_id}.prompt").unlink(missing_ok=True)

    # ── Internal ─────────────────────────────────────────────────────

    def _generate_id(self) -> str:
        """Generate a short unique job ID."""
        ts = datetime.now().strftime("%m%d_%H%M%S")
        rand = os.urandom(2).hex()
        return f"{ts}_{rand}"

    def _save_job(self, job: dict) -> None:
        """Persist job state to disk."""
        job_path = JOBS_DIR / f"{job['id']}.json"
        job_path.write_text(json.dumps(job, indent=2))

    def _refresh_running_job(self, job: dict) -> dict:
        """Check if a running job has completed.

        Checks:
        1. tmux session still alive?
        2. Exit code marker in log file?
        3. Timeout exceeded?
        """
        session_name = job["tmux_session"].replace("job-", "")

        # Check tmux session
        session_alive = self._tmux_session_exists(session_name)

        # Check for exit code marker in log
        log_path = Path(job["log_file"])
        exit_code = None
        if log_path.exists():
            try:
                # Read last 5 lines looking for exit marker
                lines = log_path.read_text(errors="replace").splitlines()
                for line in lines[-5:]:
                    if "__JOB_EXIT_CODE__=" in line:
                        exit_code = int(line.split("=")[1].strip())
                        break
            except Exception:
                pass

        # Check timeout
        started = job.get("started_at")
        timed_out = False
        if started:
            try:
                start_time = datetime.fromisoformat(started)
                if start_time.tzinfo is None:
                    start_time = start_time.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                if elapsed > job.get("timeout", JOB_TIMEOUT_S):
                    timed_out = True
            except Exception:
                pass

        # Determine new status
        if timed_out and session_alive:
            # Kill timed-out session
            try:
                subprocess.run(
                    [str(DRIVE_SH), "kill", session_name],
                    capture_output=True, timeout=10,
                )
            except Exception:
                pass
            job["status"] = "timeout"
            job["completed_at"] = datetime.now(timezone.utc).isoformat()
            job["exit_code"] = -1
            job["result_summary"] = self._extract_summary(job)
            self._save_job(job)

        elif not session_alive:
            # Session ended
            if exit_code is not None:
                job["status"] = "done" if exit_code == 0 else "failed"
                job["exit_code"] = exit_code
            else:
                job["status"] = "done"
                job["exit_code"] = 0
            job["completed_at"] = datetime.now(timezone.utc).isoformat()
            job["result_summary"] = self._extract_summary(job)
            self._save_job(job)

        return job

    def _tmux_session_exists(self, name: str) -> bool:
        """Check if a drive tmux session exists."""
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", f"drive-{name}"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _extract_summary(self, job: dict) -> str:
        """Extract a brief summary from the job's log output.

        Looks for common patterns like "completed", "error", final metrics, etc.
        """
        log_path = Path(job["log_file"])
        if not log_path.exists():
            return "No output captured."

        try:
            text = log_path.read_text(errors="replace")
            lines = text.splitlines()
        except Exception:
            return "Could not read log."

        # Filter out noise — keep substantive lines
        meaningful = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip pi harness noise
            if any(skip in stripped for skip in (
                "╭", "╰", "│", "───", "⏎", "Tool:",
                "__JOB_EXIT_CODE__", "Session '",
            )):
                continue
            meaningful.append(stripped)

        if not meaningful:
            return "Job produced no readable output."

        # Return last 15 meaningful lines as summary
        tail = meaningful[-15:]
        return "\n".join(tail)[-2000:]  # Cap at 2000 chars


# ─── Module-level singleton ──────────────────────────────────────────────────

_manager: Optional[JobManager] = None


def get_manager() -> JobManager:
    """Get or create the singleton JobManager."""
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
