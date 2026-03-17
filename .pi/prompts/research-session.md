---
description: Run a daily research session — sweeper review, creative experiments, promotion check
---
Read these skills FIRST before acting:
- atlas-research-loop: main workflow (READ THIS FIRST: /skill:atlas-research-loop)
- atlas-brain: check prior results and closed decisions BEFORE running experiments
- atlas-backtest: how to run backtests, interpret results, and record findings
- atlas-lessons: critical pitfalls to avoid (degenerate solutions, solo vs combined, etc.)
- atlas-codebase: system architecture reference

TELEGRAM: You own all notifications. Send via:
  python3 -c "import sys; sys.path.insert(0,'/root/atlas'); from utils.telegram import send_message; send_message('''YOUR_MSG''')"

Rules:
- Send ONE summary at the END of your session, not during
- ONLY send if you found something significant:
  * A strategy improved (new Sharpe > previous best) — include the numbers
  * A promotion candidate was staged — include strategy and metrics
  * A previously unknown pattern was discovered
  * Infrastructure blocked research (service down, data stale)
- If all experiments were discards and nothing improved: do NOT send
- Include: experiments run, improvements found, top finding
- Keep it under 20 lines. Use HTML formatting (<b>, <i>, <code>)

Run a daily autoresearch session. Read research/program.md first.

Your daily research tasks (in order):

1. REVIEW SWEEPER RESULTS
   - Run: leaderboard('sp500') — see what the 24/7 sweeper found
   - Check research/results/*.tsv for recent keep/discard history
   - Identify which strategies improved and which are stuck

2. CREATIVE RESEARCH (what the sweeper can't do)
   Pick 2-3 of these based on what the leaderboard shows:
   a) Screen untested sandbox strategies: quick_check() then baseline if alive
   b) Try parameter combos the grid missed (pairs, triples, unusual values)
   c) Run combined_test() on any strategy with Sharpe > 0.3
   d) Test radical changes (disable filters, flip directions, extreme values)

3. PROMOTION CHECK
   Any strategy with solo Sharpe > 0.3 AND passing combined test:
   - Stage candidate config in config/candidates/
   - Send promotion request via Telegram (NEVER auto-promote)

4. SEND SUMMARY via Telegram:
   - Sweeper overnight results (experiments run, improvements found)
   - Your creative research results
   - Current leaderboard top 5
   - What needs human attention (promotions, stuck strategies)

Budget: up to 8 hours. Run as many experiments as time allows.
Focus on strategies the sweeper hasn't cracked yet.
