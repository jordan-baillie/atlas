# Research Deep Dive

Focus on a single strategy for intensive analysis. Use when a strategy shows promise but needs deeper investigation, or when you want to understand why a strategy is underperforming.

## Instructions
- Time limit: 45 minutes
- You have full access to run backtests via `research.loop.ResearchSession`
- Try at most 10 experiments — quality over quantity
- Document every finding, even negative results
- If you find a significant improvement (Sharpe delta > +0.05), run `combined_test()` to check portfolio fit

## Tasks
1. Identify the target strategy:
   - If a strategy name was provided in the prompt, use that
   - Otherwise, pick the most promising candidate from the leaderboard that hasn't been deeply explored
2. Load current state: `ResearchSession(strategy, 'sp500')` then `baseline()`
3. Analyze the parameter history in `research/best/<strategy>.json`:
   - What parameters have been swept?
   - What ranges worked best?
   - Are there interaction effects between parameters?
4. Run targeted experiments:
   - Test values between known good points (interpolation)
   - Test parameter combinations that haven't been tried together
   - Try disabling/enabling filters
   - Try extreme values if the search has been too conservative
5. For each experiment, use `session.experiment()` and follow keep/discard recommendations
6. If Sharpe > 0.3, run `session.combined_test()` to check portfolio impact

## Deliverables
- Strategy name and starting baseline metrics
- Summary of experiments run (parameter, value, result, verdict)
- Key findings: what works, what doesn't, interaction effects
- Final metrics vs starting baseline
- Recommendation: continue optimizing / ready for OOS / retire
