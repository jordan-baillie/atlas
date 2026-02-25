#!/usr/bin/env python3
"""Add Moomoo login credentials to ~/.atlas-secrets.json."""
import hashlib
import sys
import os
from getpass import getpass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from brokers.secrets import _load_secrets_file, save_secrets_file, SECRETS_FILE

print("=" * 50)
print("  Add Moomoo Login Credentials")
print("=" * 50)
print(f"\nSecrets file: {SECRETS_FILE}\n")

# Load existing
try:
    secrets = _load_secrets_file()
except Exception:
    secrets = {}

# Account
existing_acc = secrets.get("MOOMOO_LOGIN_ACCOUNT", "")
if existing_acc:
    mask = existing_acc[:3] + "***"
    print(f"  Current account: {mask}")
    acc = input("  New account (Enter to keep): ").strip()
    if not acc:
        acc = existing_acc
else:
    acc = input("  Moomoo login account (ID/phone/email): ").strip()

if not acc:
    print("❌ Account required")
    sys.exit(1)

# Password → MD5
existing_md5 = secrets.get("MOOMOO_LOGIN_PWD_MD5", "")
if existing_md5:
    print(f"  Current password MD5: {existing_md5[:6]}***")
    change = input("  Change password? (y/N): ").strip().lower()
    if change == "y":
        pwd = getpass("  Moomoo login password: ")
        md5 = hashlib.md5(pwd.encode()).hexdigest()
    else:
        md5 = existing_md5
else:
    pwd = getpass("  Moomoo login password: ")
    md5 = hashlib.md5(pwd.encode()).hexdigest()

print(f"\n  Account:  {acc[:3]}***")
print(f"  MD5 hash: {md5[:6]}***")

secrets["MOOMOO_LOGIN_ACCOUNT"] = acc
secrets["MOOMOO_LOGIN_PWD_MD5"] = md5

save_secrets_file(secrets)
print(f"\n✅ Saved to {SECRETS_FILE}")
print("\nNext: python3 scripts/start_opend.py --background")
