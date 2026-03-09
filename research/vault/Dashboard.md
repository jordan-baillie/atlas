# Atlas Research Vault — Index

> Agent knowledge base. Start with `KNOWLEDGE_BASE.md` for full system state.

## Primary Reference
- **[KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)** — Complete research state, strategy cards, patterns, decisions, priorities. READ THIS FIRST.

## Deep-Dive Notes

### Strategies (14 cards)
| Strategy | Status | File |
|----------|--------|------|
| Mean Reversion | ✅ active | `Strategies/Mean Reversion.md` |
| Trend Following | ✅ active | `Strategies/Trend Following.md` |
| Opening Gap | ✅ active | `Strategies/Opening Gap.md` |
| Momentum Breakout | ⏸️ dormant | `Strategies/Momentum Breakout.md` |
| Short Term MR | ⏸️ dormant | `Strategies/Short Term MR.md` |
| Sector Rotation | ⏸️ dormant | `Strategies/Sector Rotation.md` |
| BB Squeeze | ⏸️ dormant | `Strategies/Bollinger Band Squeeze.md` |
| ConnorsRSI2 | ❌ failed | `Strategies/ConnorsRSI2.md` |
| Lower Band Reversion | ❌ failed | `Strategies/Lower Band Reversion.md` |
| Triple RSI | ❌ failed | `Strategies/Triple RSI.md` |
| MTF Momentum | 🔧 blocked | `Strategies/MTF Momentum.md` |
| SMA-200 Filter | ✅ promoted | `Strategies/SMA-200 Filter.md` |
| Portfolio Filter | 🔧 various | `Strategies/Portfolio Filter.md` |
| Combined Portfolio | — baseline | `Strategies/Combined Portfolio.md` |

### Experiments (35 unique)
`Experiments/{experiment_id}.md` — Each has YAML frontmatter with metrics + full hypothesis/verdict/learnings.

### Waves (5 complete)
`Waves/Wave {1-5}.md` — Theme, experiment list, key findings.

### Patterns (5 confirmed)
`Patterns/{name}.md` — Research patterns that must never be violated.

## Regenerate
```bash
python3 scripts/build_obsidian_vault.py --force
```
