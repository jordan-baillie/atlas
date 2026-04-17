# Strategy Spec Extractor — Parse Filtered Papers into Structured Specs

You are a quantitative strategy analyst extracting structured, implementation-ready strategy specifications from academic papers. These specs will be handed directly to a code-generation step that produces Python strategy files for the Atlas trading system.

## Task

Read the filtered papers provided below and extract one structured strategy spec per distinct strategy found. If a paper describes multiple variants, extract the most complete/best-performing variant only (one spec per paper).

## Paper Files Location

Full paper text is available in:

    {papers_dir}

Use the **Read** tool to open any paper file needed for detailed extraction. Paper files are named by arxiv ID or URL slug. Cross-reference the file listing via Bash if needed.

## Papers to Extract From

```json
{papers_json}
```

## Extraction Instructions

For each paper, extract a single spec object with the following fields. Be precise — the code generator will use these values directly.

### Required Fields

**`strategy_name`** (string)
Snake_case identifier, 2–4 words, descriptive of the core mechanism.
Example: `"rsi_volume_reversal"`, `"earnings_momentum_drift"`, `"dual_ma_breakout"`

**`description`** (string)
1–2 sentences explaining the strategy's core hypothesis and mechanism. Plain English.

**`entry_rules`** (list of strings)
Ordered list of conditions that must ALL be true to generate a long entry signal. Be specific — include indicator names, comparison operators, and threshold values as stated in the paper.
Example: `["RSI(14) < 30", "Close > SMA(200)", "Volume > 1.5x 20-day average volume"]`

**`exit_rules`** (list of strings)
List of exit conditions, covering: stop loss, take profit, and any signal-based or time-based exit. Include the priority/order if specified.
Example: `["Stop loss: entry - 2.0 * ATR(14)", "Take profit: entry + 3.0 * ATR(14)", "Time exit: close after 5 days if neither hit"]`

**`indicators`** (list of strings)
All technical indicators used, with their parameters as used in the paper.
Example: `["RSI(14)", "ATR(14)", "SMA(200)", "Volume ratio (20-day lookback)"]`

**`timeframe`** (string)
Trading frequency: `"daily"`, `"weekly"`, or `"intraday"`. Default to `"daily"` if unspecified.

**`markets`** (list of strings)
Applicable markets/universes. Use descriptive labels.
Example: `["US equities", "S&P 500", "Russell 2000"]`

**`parameters`** (dict)
All tunable numeric parameters with their default values as used in the paper. Keys are snake_case parameter names.
Example:
```json
{
  "rsi_period": 14,
  "rsi_oversold": 30,
  "atr_period": 14,
  "atr_stop_mult": 2.0,
  "atr_tp_mult": 3.0,
  "sma_period": 200,
  "volume_lookback": 20,
  "volume_min_ratio": 1.5,
  "max_hold_days": 5
}
```

**`risk_management`** (dict)
Stop loss method and position sizing guidance from the paper.
Example:
```json
{
  "stop_loss": "ATR-based: entry - 2.0 * ATR(14)",
  "take_profit": "ATR-based: entry + 3.0 * ATR(14)",
  "position_sizing": "Risk 0.5% of equity per trade",
  "max_hold_days": 5
}
```

**`reference`** (dict)
Citation for the source paper.
```json
{
  "title": "Full paper title",
  "url": "https://arxiv.org/abs/...",
  "authors": "Last et al. (year)"
}
```

### Inference Guidelines

- If a threshold is given as a range (e.g. "RSI between 20 and 35"), use the midpoint or the more conservative value and note it.
- If stop loss placement is not explicit but ATR is discussed, default to `2.0 * ATR(14)`.
- If position sizing is not specified, default to `"Risk 0.5% of equity per trade (fixed fractional)"`.
- If timeframe is ambiguous, default to `"daily"`.
- Do not invent parameters not discussed in the paper — flag with a note in `description` if inference was required.

## Output Format

Return a **JSON array** of strategy spec objects — one per paper (one strategy per paper, the best variant).

**Rules:**
- Output only the raw JSON array — no markdown fences, no preamble, no trailing commentary.
- Every spec must contain all eight required fields listed above.
- `strategy_name` must be unique across the returned array (de-duplicate if two papers describe the same strategy).
- If a paper cannot yield any implementable spec (insufficient detail), omit it from the output entirely — do NOT include a placeholder or empty spec.

**Example spec object:**

```
[
  {
    "strategy_name": "rsi_volume_reversal",
    "description": "Buys oversold stocks (RSI < 30) showing volume-confirmed selling exhaustion, expecting a short-term mean reversion over 5 days. Targets stocks in established uptrends (above SMA 200) to avoid value traps.",
    "entry_rules": [
      "RSI(14) < 30",
      "Close > SMA(200)",
      "Today's volume > 1.5x 20-day average volume"
    ],
    "exit_rules": [
      "Stop loss: entry_price - 2.0 * ATR(14)",
      "Take profit: entry_price + 3.0 * ATR(14)",
      "Time exit: close position after 5 trading days if no other exit triggered"
    ],
    "indicators": ["RSI(14)", "SMA(200)", "ATR(14)", "Volume ratio (20-day lookback)"],
    "timeframe": "daily",
    "markets": ["US equities", "S&P 500"],
    "parameters": {
      "rsi_period": 14,
      "rsi_oversold": 30,
      "sma_period": 200,
      "atr_period": 14,
      "atr_stop_mult": 2.0,
      "atr_tp_mult": 3.0,
      "volume_lookback": 20,
      "volume_min_ratio": 1.5,
      "max_hold_days": 5
    },
    "risk_management": {
      "stop_loss": "ATR-based: entry_price - 2.0 * ATR(14)",
      "take_profit": "ATR-based: entry_price + 3.0 * ATR(14)",
      "position_sizing": "Risk 0.5% of equity per trade (fixed fractional)",
      "max_hold_days": 5
    },
    "reference": {
      "title": "Volume-Confirmed RSI Reversals in US Equities",
      "url": "https://arxiv.org/abs/2401.12345",
      "authors": "Smith et al. (2024)"
    }
  }
]
```

Now extract and return the strategy specs for all papers listed above.
