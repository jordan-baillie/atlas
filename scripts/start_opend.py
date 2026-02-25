#!/usr/bin/env python3
"""Start FutuOpenD using credentials from ~/.atlas-secrets.json.

Usage:
    python3 scripts/start_opend.py              # foreground
    python3 scripts/start_opend.py --background  # daemon mode
    python3 scripts/start_opend.py --stop        # kill running instance
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from brokers.secrets import get_secret

OPEND_DIR = "/opt/Futu_OpenD_9.6.5618_Ubuntu18.04/Futu_OpenD_9.6.5618_Ubuntu18.04"
OPEND_BIN = os.path.join(OPEND_DIR, "FutuOpenD")
PID_FILE = "/tmp/futu_opend.pid"


def get_pid() -> int | None:
    """Read PID from file if process is still alive."""
    if not os.path.exists(PID_FILE):
        return None
    try:
        pid = int(open(PID_FILE).read().strip())
        os.kill(pid, 0)  # check alive
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        os.unlink(PID_FILE)
        return None


def stop():
    pid = get_pid()
    if pid:
        print(f"Stopping OpenD (pid {pid})...")
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                print("✅ OpenD stopped")
                if os.path.exists(PID_FILE):
                    os.unlink(PID_FILE)
                return True
        print("⚠️  Force killing...")
        os.kill(pid, signal.SIGKILL)
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)
        return True
    else:
        print("OpenD not running")
        return False


def start(background: bool = False):
    # Check not already running
    pid = get_pid()
    if pid:
        print(f"OpenD already running (pid {pid})")
        return

    # Check binary exists
    if not os.path.exists(OPEND_BIN):
        print(f"❌ OpenD binary not found: {OPEND_BIN}")
        sys.exit(1)

    # Load credentials from secrets
    account = get_secret("MOOMOO_LOGIN_ACCOUNT")
    pwd_md5 = get_secret("MOOMOO_LOGIN_PWD_MD5")

    if not account:
        print("❌ MOOMOO_LOGIN_ACCOUNT not found in secrets")
        print("   Add it: python3 -c \"")
        print("     import json, sys; sys.path.insert(0,'.');\\ ")
        print("     from brokers.secrets import _load_secrets_file, save_secrets_file;\\ ")
        print("     s=_load_secrets_file(); s['MOOMOO_LOGIN_ACCOUNT']='your_account';\\ ")
        print("     s['MOOMOO_LOGIN_PWD_MD5']='your_md5_hash'; save_secrets_file(s)\"")
        sys.exit(1)

    if not pwd_md5:
        print("❌ MOOMOO_LOGIN_PWD_MD5 not found in secrets")
        print("   Generate MD5: echo -n 'your_password' | md5sum | cut -d' ' -f1")
        sys.exit(1)

    # Build command
    cmd = [
        OPEND_BIN,
        f"-login_account={account}",
        f"-login_pwd_md5={pwd_md5}",
        "-lang=en",
        "-api_port=11111",
        f"-cfg_file={os.path.join(OPEND_DIR, 'FutuOpenD.xml')}",
    ]

    if background:
        cmd.append("-console=0")

    print(f"Starting OpenD (account: {account[:3]}***)")
    print(f"  API port: 11111")
    print(f"  Mode: {'background' if background else 'foreground'}")

    if background:
        log_file = "/tmp/futu_opend.log"
        with open(log_file, "a") as log:
            proc = subprocess.Popen(
                cmd,
                cwd=OPEND_DIR,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))
        print(f"  PID: {proc.pid}")
        print(f"  Log: {log_file}")

        # Wait for port to be ready
        print("  Waiting for port 11111...", end="", flush=True)
        for i in range(30):
            time.sleep(1)
            import socket
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect(("127.0.0.1", 11111))
                s.close()
                print(f" ready ({i+1}s)")
                print("✅ OpenD started successfully")
                return
            except (ConnectionRefusedError, socket.timeout, OSError):
                print(".", end="", flush=True)

        print(" timeout!")
        print("⚠️  Port not ready after 30s. Check /tmp/futu_opend.log")
    else:
        # Foreground — exec replaces this process
        os.chdir(OPEND_DIR)
        os.execv(OPEND_BIN, cmd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start/stop FutuOpenD")
    parser.add_argument("--background", "-b", action="store_true",
                        help="Run in background")
    parser.add_argument("--stop", action="store_true",
                        help="Stop running instance")
    args = parser.parse_args()

    if args.stop:
        stop()
    else:
        start(background=args.background)
