---
description: Review today's trading plan — entries, exits, risk exposure, strategy distribution
---
Review today's trading plan for the ${1:-sp500} market.

1. Find today's plan file:
   plans/plan_${1:-sp500}_$(date '+%Y-%m-%d').json
   If it doesn't exist yet, check for the most recent plan in plans/.

2. Summarize the plan using atlas_artifacts_summarize on the plan JSON.

3. Show a clear breakdown of:
   - Proposed entries: symbol, strategy, direction, position size, entry/stop/target
   - Proposed exits: any active positions being closed
   - Total capital exposure and number of open positions after execution
   - Strategy distribution (which strategies generated signals today)
   - Risk metrics: max drawdown, portfolio heat

4. Check the plan gate status:
   Use atlas_risk_check_plan_gate to evaluate whether the plan is ready to approve.
   Report the gate result and any blocking conditions.

5. Highlight anything unusual:
   - Concentrated exposure in a single sector or strategy
   - Unusually large or small position sizes
   - Signals near earnings, high-IV events, or known risk dates
   - Any rejected signals (if visible in the plan)

This is for manual review — conversational tone is fine. You don't need to approve or execute anything.
If the user wants to approve after reviewing, they can run: /approve ${1:-sp500}
