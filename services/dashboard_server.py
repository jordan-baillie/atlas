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
import logging
import os
import secrets
import signal
import sys
import threading
import time
import traceback
from datetime import datetime
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("dashboard_server")

# Rate limiting for expensive endpoints
_last_evaluate_time = 0.0

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

    plan = plan_gen.approve_plan(trade_date)
    if not plan or plan.get("status") != "APPROVED":
        return {"ok": False, "error": "Failed to approve plan"}

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

        # Only mark EXECUTED if at least one order succeeded or plan had no orders
        entries_ok = sum(1 for e in report.get("entries", []) if e.get("success"))
        exits_ok = sum(1 for e in report.get("exits", []) if e.get("success"))
        total_entries = len(report.get("entries", []))
        total_exits = len(report.get("exits", []))

        if report.get("error"):
            # execute_plan returned an error (e.g. status check failed)
            return {"ok": False, "error": report["error"]}

        # Mark plan as executed
        plan["status"] = "EXECUTED"
        plan["executed_at"] = __import__("datetime").datetime.now().isoformat()
        from brokers.plan import TradePlanGenerator
        from brokers.live_portfolio import LivePortfolio
        tpg = TradePlanGenerator(LivePortfolio(config, market_id=market_id), config)
        tpg._save_plan(plan, trade_date)

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

    def end_headers(self):
        """Add cache control headers based on response content type."""
        path = self.path.split('?')[0] if hasattr(self, 'path') else ''
        if path.endswith('.json'):
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
        elif path.endswith('.html') or path in ('/', ''):
            self.send_header('Cache-Control', 'no-cache')
        else:
            self.send_header('Cache-Control', 'public, max-age=3600')
        super().end_headers()

    def do_GET(self):
        if not self._check_auth():
            return self._send_401()
        if self.path.startswith("/api/stream"):
            return self._handle_sse_stream()
        if self.path.startswith("/api/prices"):
            return self._handle_prices()
        if self.path.startswith("/api/snapshot"):
            return self._handle_snapshot()
        if self.path.startswith("/api/monitor"):
            return self._send_json(410, {"error": "Monitor tab removed"})
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
        elif self.path.startswith("/api/monitor"):
            self._send_json(410, {"error": "Monitor tab removed"})
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

    def _handle_sse_stream(self):
        """GET /api/stream — Server-Sent Events stream of live Alpaca data.

        Pushes events whenever the poller state changes:
          event: snapshot
          data: {account, positions, orders, market_clock, summary, timestamp}

        The client connects once and receives updates every 10s (market open)
        or 60s (market closed).
        """
        try:
            from dashboard.alpaca_stream import get_state, get_seq

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            last_seq = -1
            while True:
                current_seq = get_seq()
                if current_seq != last_seq:
                    state = get_state()
                    last_seq = current_seq

                    payload = json.dumps(state, default=str)
                    self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode())
                    self.wfile.flush()

                time.sleep(2)  # Check for changes every 2s

        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected
        except Exception as e:
            logger.warning("SSE stream error: %s", e)

    def _handle_snapshot(self):
        """GET /api/snapshot — One-shot JSON of current Alpaca state."""
        try:
            from dashboard.alpaca_stream import get_state
            state = get_state()
            self._send_json(200, state)
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_prices(self):
        """GET /api/prices — pre-computed P&L for open positions.

        Returns the new simple format with server-computed P&L per
        position.  The frontend does NO math — it just swaps values.

        Query params:
          ?tickers=AAPL,REH.AX   — legacy: fall back to raw quotes
        """
        try:
            sys.path.insert(0, str(PROJECT_ROOT))

            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            tickers_param = params.get("tickers", [""])[0]

            if tickers_param:
                # Legacy mode: specific tickers requested → return raw quotes
                from dashboard.live_prices import fetch_prices, get_cache_stats
                tickers = [t.strip() for t in tickers_param.split(",") if t.strip()]
                quotes = fetch_prices(tickers)
                response = {
                    "ok": True,
                    "timestamp": datetime.now().isoformat(),
                    "quotes": quotes,
                    "cache": get_cache_stats(),
                    "ticker_count": len(quotes),
                }
            else:
                # New mode: pre-computed P&L for all positions
                from dashboard.live_prices import get_live_prices_with_pnl
                simple_path = str(SERVE_DIR / "simple-dashboard-data.json")
                response = get_live_prices_with_pnl(simple_path)

            self._send_json(200, response)

        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

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
        global _last_evaluate_time
        now = time.time()
        if now - _last_evaluate_time < 10:
            return self._send_json(429, {
                "error": "Rate limited — wait 10 seconds between evaluations",
                "retry_after": round(10 - (now - _last_evaluate_time), 1),
            })
        _last_evaluate_time = now

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

    # Start Alpaca background poller for SSE streaming
    try:
        from dashboard.alpaca_stream import start as start_stream
        start_stream(interval_open=10, interval_closed=60)
        print("Alpaca live poller started", flush=True)
    except Exception as e:
        print(f"⚠️ Alpaca poller failed to start: {e}", flush=True)
        print("  Dashboard will serve static JSON only", flush=True)

    handler = partial(AuthHandler, directory=str(SERVE_DIR))

    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True
        # Use threading to handle SSE streams without blocking other requests
        request_queue_size = 32

    # Use ThreadingMixIn so SSE connections don't block the server
    from http.server import ThreadingHTTPServer

    with ThreadingHTTPServer((BIND, PORT), handler) as server:
        server.allow_reuse_address = True
        print(
            f"Atlas dashboard serving on {BIND}:{PORT} "
            f"(auth: {user}) pid={os.getpid()}",
            flush=True,
        )
        server.serve_forever()


if __name__ == "__main__":
    main()
