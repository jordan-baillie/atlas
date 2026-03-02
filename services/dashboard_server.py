#!/usr/bin/env python3
"""Atlas Dashboard Server — HTTP Basic Auth protected.

Serves the dashboard static files behind HTTP Basic Auth.
Includes API endpoints for plan approval/rejection.

Credentials from ~/.atlas-secrets.json:
    dashboard_user, dashboard_pass

Run:
    python3 services/dashboard_server.py              # foreground
    systemctl start atlas-dashboard                   # systemd
"""

import base64
import json
import os
import secrets
import signal
import sys
import threading
import traceback
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

signal.signal(signal.SIGHUP, signal.SIG_IGN)

PROJECT_ROOT = Path("/root/atlas")
SECRETS_PATH = Path.home() / ".atlas-secrets.json"
SERVE_DIR = PROJECT_ROOT / "dashboard" / "data"
BIND = "127.0.0.1"
PORT = 8899


def _load_credentials() -> tuple[str, str]:
    if not SECRETS_PATH.exists():
        raise ValueError(f"Secrets file not found: {SECRETS_PATH}")
    with open(SECRETS_PATH) as f:
        s = json.load(f)
    user = s.get("dashboard_user", "")
    pw = s.get("dashboard_pass", "")
    if not user or not pw:
        raise ValueError(
            "Set dashboard_user and dashboard_pass in ~/.atlas-secrets.json"
        )
    return user, pw


# ── Plan approval/execution logic ────────────────────────────
def _approve_and_execute(trade_date: str, market_id: str) -> dict:
    """Approve a plan and execute it. Returns result dict."""
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(PROJECT_ROOT)

    from utils.config import get_active_config
    from brokers.live_portfolio import LivePortfolio
    from brokers.plan import TradePlanGenerator

    config = get_active_config(market_id)

    # Load & approve the plan (live broker is sole source of truth)
    portfolio = LivePortfolio(config, market_id=market_id)
    plan_gen = TradePlanGenerator(portfolio, config)
    plan = plan_gen.load_plan(trade_date)

    if not plan:
        return {"ok": False, "error": f"No plan found for {trade_date}"}

    if plan.get("status") == "EXECUTED":
        return {"ok": False, "error": "Plan already executed"}

    if plan.get("status") == "APPROVED":
        return {"ok": False, "error": "Plan already approved (awaiting execution)"}

    plan_gen.approve_plan(trade_date)

    # Always execute live — live broker is sole source of truth
    result = _execute_live(plan, trade_date, config, market_id)

    # Regenerate dashboard data
    try:
        from dashboard.generate_data import generate
        generate()
    except Exception as e:
        print(f"Dashboard regen failed: {e}")

    return result


def _execute_live(plan, trade_date, config, market_id) -> dict:
    """Execute via live broker."""
    from brokers.live_executor import LiveExecutor
    from brokers.live_portfolio import LivePortfolio

    executor = LiveExecutor(config)
    if not executor.connect():
        return {"ok": False, "error": f"Failed to connect to broker"}

    try:
        report = executor.execute_plan(plan, trade_date)

        # Mark plan as executed
        plan["status"] = "EXECUTED"
        plan["executed_at"] = __import__("datetime").datetime.now().isoformat()
        from brokers.plan import TradePlanGenerator
        from brokers.live_portfolio import LivePortfolio
        tpg = TradePlanGenerator(LivePortfolio(config, market_id=market_id), config)
        tpg._save_plan(plan, trade_date)

        entries_ok = sum(1 for e in report.get("entries", []) if e.get("success"))
        exits_ok = sum(1 for e in report.get("exits", []) if e.get("success"))
        total_entries = len(report.get("entries", []))
        total_exits = len(report.get("exits", []))

        return {
            "ok": True,
            "mode": "live",
            "market_id": market_id,
            "entries": f"{entries_ok}/{total_entries}",
            "exits": f"{exits_ok}/{total_exits}",
            "report": report,
        }
    finally:
        executor.disconnect()


def _reject_plan(trade_date: str, market_id: str) -> dict:
    """Reject a plan (mark as REJECTED, don't execute)."""
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(PROJECT_ROOT)

    from utils.config import get_active_config
    from brokers.live_portfolio import LivePortfolio
    from brokers.plan import TradePlanGenerator

    config = get_active_config(market_id)
    portfolio = LivePortfolio(config, market_id=market_id)
    plan_gen = TradePlanGenerator(portfolio, config)
    plan = plan_gen.load_plan(trade_date)

    if not plan:
        return {"ok": False, "error": f"No plan found for {trade_date}"}

    plan["status"] = "REJECTED"
    plan["rejected_at"] = __import__("datetime").datetime.now().isoformat()
    plan_gen._save_plan(plan, trade_date)

    # Regenerate dashboard data
    try:
        from dashboard.generate_data import generate
        generate()
    except Exception:
        pass

    return {"ok": True, "status": "REJECTED"}


# ── HTTP Handler ─────────────────────────────────────────────
class AuthHandler(SimpleHTTPRequestHandler):
    """HTTP handler with Basic Auth + API endpoints."""

    expected_user = ""
    expected_pass = ""

    def do_GET(self):
        if not self._check_auth():
            return self._send_401()
        if self.path.startswith("/api/monitor"):
            return self._handle_monitor_get()
        super().do_GET()

    def do_HEAD(self):
        if not self._check_auth():
            return self._send_401()
        super().do_HEAD()

    def do_POST(self):
        if not self._check_auth():
            return self._send_401()

        if self.path == "/api/approve":
            self._handle_approve()
        elif self.path == "/api/reject":
            self._handle_reject()
        elif self.path == "/api/monitor/positions":
            self._handle_monitor_add_position()
        elif self.path == "/api/monitor/evaluate":
            self._handle_monitor_evaluate()
        elif self.path.startswith("/api/monitor/positions/") and self.path.endswith("/toggle"):
            self._handle_monitor_toggle_condition()
        elif self.path.startswith("/api/monitor/positions/") and self.path.endswith("/close"):
            self._handle_monitor_close_position()
        elif self.path.startswith("/api/monitor/positions/") and self.path.endswith("/note"):
            self._handle_monitor_add_note()
        elif self.path == "/api/monitor/templates":
            self._handle_monitor_save_template()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        if not self._check_auth():
            return self._send_401()
        if self.path.startswith("/api/monitor/positions/"):
            pos_id = self.path.split("/")[-1]
            sys.path.insert(0, str(PROJECT_ROOT))
            from monitor.models import PositionStore
            store = PositionStore()
            ok = store.delete_position(pos_id)
            self._send_json(200 if ok else 404, {"ok": ok})
        elif self.path.startswith("/api/monitor/templates/"):
            tmpl_id = self.path.split("/")[-1]
            sys.path.insert(0, str(PROJECT_ROOT))
            from monitor.models import PositionStore
            store = PositionStore()
            ok = store.delete_template(tmpl_id)
            self._send_json(200 if ok else 404, {"ok": ok})
        else:
            self._send_json(404, {"error": "Not found"})

    def _handle_approve(self):
        try:
            body = self._read_body()
            trade_date = body.get("trade_date", "")
            market_id = body.get("market_id", "")
            if not trade_date or not market_id:
                return self._send_json(400, {"error": "trade_date and market_id required"})

            # Run in thread to avoid blocking (broker I/O can be slow)
            result = {"pending": True}

            def _run():
                nonlocal result
                try:
                    result = _approve_and_execute(trade_date, market_id)
                except Exception as e:
                    traceback.print_exc()
                    result = {"ok": False, "error": str(e)}

            t = threading.Thread(target=_run)
            t.start()
            t.join(timeout=60)  # 60s max for broker execution

            if result.get("pending"):
                return self._send_json(504, {"error": "Execution timed out (still running in background)"})

            status = 200 if result.get("ok") else 400
            self._send_json(status, result)

        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def _handle_reject(self):
        try:
            body = self._read_body()
            trade_date = body.get("trade_date", "")
            market_id = body.get("market_id", "")
            if not trade_date or not market_id:
                return self._send_json(400, {"error": "trade_date and market_id required"})

            result = _reject_plan(trade_date, market_id)
            status = 200 if result.get("ok") else 400
            self._send_json(status, result)
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    # ── Monitor API handlers ─────────────────────────────────

    def _handle_monitor_get(self):
        """GET /api/monitor — full monitor state."""
        sys.path.insert(0, str(PROJECT_ROOT))
        from monitor.models import PositionStore
        from dataclasses import asdict
        store = PositionStore()
        positions = store.load_positions()
        templates = store.load_templates()
        alerts = store.load_alerts(50)
        summary = store.get_summary()
        self._send_json(200, {
            "positions": [asdict(p) for p in positions],
            "templates": [asdict(t) for t in templates],
            "alerts": alerts,
            "summary": summary,
        })

    def _handle_monitor_add_position(self):
        """POST /api/monitor/positions — add a new position."""
        sys.path.insert(0, str(PROJECT_ROOT))
        from monitor.models import Position, PositionStore
        from dataclasses import asdict
        body = self._read_body()
        try:
            pos = Position(**{k: v for k, v in body.items()
                              if k in Position.__dataclass_fields__})
            pos.update_health()
            store = PositionStore()
            store.add_position(pos)
            self._send_json(200, {"ok": True, "position": asdict(pos)})
        except Exception as e:
            self._send_json(400, {"ok": False, "error": str(e)})

    def _handle_monitor_evaluate(self):
        """POST /api/monitor/evaluate — evaluate all positions now."""
        sys.path.insert(0, str(PROJECT_ROOT))
        import threading
        result = {"pending": True}
        def _run():
            nonlocal result
            try:
                from monitor.evaluator import evaluate_all
                result = evaluate_all(send_telegram=False)
                result["ok"] = True
            except Exception as e:
                result = {"ok": False, "error": str(e)}
        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=120)
        if result.get("pending"):
            self._send_json(504, {"error": "Evaluation timed out"})
        else:
            self._send_json(200, result)

    def _handle_monitor_toggle_condition(self):
        """POST /api/monitor/positions/{id}/toggle — toggle a manual condition."""
        parts = self.path.split("/")
        pos_id = parts[4]  # /api/monitor/positions/{id}/toggle
        sys.path.insert(0, str(PROJECT_ROOT))
        from monitor.models import PositionStore
        from dataclasses import asdict
        body = self._read_body()
        cond_id = body.get("condition_id", "")
        new_status = body.get("status", "passing")
        store = PositionStore()
        pos = store.get_position(pos_id)
        if not pos:
            return self._send_json(404, {"error": "Position not found"})
        for c in pos.conditions:
            if c.id == cond_id:
                c.status = new_status
                break
        pos.update_health()
        store.update_position(pos)
        self._send_json(200, {"ok": True, "health_score": pos.health_score})

    def _handle_monitor_close_position(self):
        """POST /api/monitor/positions/{id}/close — close a position."""
        parts = self.path.split("/")
        pos_id = parts[4]
        sys.path.insert(0, str(PROJECT_ROOT))
        from monitor.models import PositionStore
        from datetime import datetime
        body = self._read_body()
        store = PositionStore()
        pos = store.get_position(pos_id)
        if not pos:
            return self._send_json(404, {"error": "Position not found"})
        pos.status = "closed"
        pos.closed_at = datetime.now().isoformat(timespec="seconds")
        pos.close_price = body.get("close_price", pos.current_price)
        pos.close_reason = body.get("reason", "manual")
        store.update_position(pos)
        self._send_json(200, {"ok": True})

    def _handle_monitor_add_note(self):
        """POST /api/monitor/positions/{id}/note — add a note."""
        parts = self.path.split("/")
        pos_id = parts[4]
        sys.path.insert(0, str(PROJECT_ROOT))
        from monitor.models import PositionStore
        from datetime import datetime
        body = self._read_body()
        store = PositionStore()
        pos = store.get_position(pos_id)
        if not pos:
            return self._send_json(404, {"error": "Position not found"})
        pos.notes.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "text": body.get("text", ""),
        })
        store.update_position(pos)
        self._send_json(200, {"ok": True})

    def _handle_monitor_save_template(self):
        """POST /api/monitor/templates — save a template."""
        sys.path.insert(0, str(PROJECT_ROOT))
        from monitor.models import Template, PositionStore
        from dataclasses import asdict
        body = self._read_body()
        try:
            tmpl = Template(**{k: v for k, v in body.items()
                               if k in Template.__dataclass_fields__})
            store = PositionStore()
            store.save_template(tmpl)
            self._send_json(200, {"ok": True, "template": asdict(tmpl)})
        except Exception as e:
            self._send_json(400, {"ok": False, "error": str(e)})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw)

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_401(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Atlas Dashboard"')
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>401 Unauthorized</h1>")

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            user, pw = decoded.split(":", 1)
        except Exception:
            return False
        user_ok = secrets.compare_digest(user, self.expected_user)
        pw_ok = secrets.compare_digest(pw, self.expected_pass)
        return user_ok and pw_ok

    def log_message(self, fmt, *args):
        # Only log API calls, not static file requests
        first = str(args[0]) if args else ""
        if "/api/" in first:
            print(f"[API] {first}", flush=True)


def main():
    try:
        user, pw = _load_credentials()
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    AuthHandler.expected_user = user
    AuthHandler.expected_pass = pw

    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(PROJECT_ROOT)

    handler = partial(AuthHandler, directory=str(SERVE_DIR))

    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True

    with ReusableHTTPServer((BIND, PORT), handler) as server:
        print(
            f"Atlas dashboard serving on {BIND}:{PORT} "
            f"(auth: {user}) pid={os.getpid()}",
            flush=True,
        )
        server.serve_forever()


if __name__ == "__main__":
    main()
