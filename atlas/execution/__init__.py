"""atlas.execution — the forge->live loop.

Daily flow (atlas-live-shadow timer -> ops/forward-paper.sh):
record_returns -> crucible weight refresh -> daily.py rebalance per deployed
strategy via TargetExecutor (kill-switch enforced fail-closed inside).
deploy_pass() in providers.py is the entry point Crucible subprocess-calls.
"""
