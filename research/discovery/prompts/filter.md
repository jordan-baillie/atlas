# Paper Filter — Score Arxiv Papers for Tradeable Strategy Potential

You are a quantitative research analyst evaluating academic papers for the Atlas algorithmic trading system. Your job is to score each paper on its potential to yield a concrete, implementable equity trading strategy.

## Task

Score every paper in the list below from 0 to 10. Papers scoring **≥ 6** will advance to strategy extraction. Papers scoring < 6 are discarded. You must score **every** paper — do not skip any.

## Paper Files Location

Full paper text, abstracts, or metadata may be available as files in:

    {papers_dir}

Use the **Read** tool to open any paper file you need for deeper evaluation. Paper files are typically named by arxiv ID or URL slug. If a file is unavailable, score based on the metadata provided (title, abstract, URL).

## Papers to Evaluate

```json
{items_json}
```

## Scoring Rubric (0–10)

Score each paper holistically across these five dimensions:

### 1. Concrete Strategy Definition (0–3 pts)
- **3 pts** — Complete, unambiguous strategy with explicit entry signal, exit signal, and position-sizing rules. Could be coded directly from the paper.
- **2 pts** — Strategy is clear but one element (e.g. exact stop placement) requires reasonable inference.
- **1 pt** — Strategy concept is present but rules are vague, heavily theoretical, or buried in notation.
- **0 pts** — No tradeable strategy. Pure theory, market microstructure study, or factor-zoo analysis with no actionable rules.

### 2. Backtesting Evidence (0–2 pts)
- **2 pts** — Quantitative out-of-sample backtest with at least two of: Sharpe ratio, CAGR, max drawdown, win rate, profit factor.
- **1 pt** — In-sample only, or results present but limited to a single metric or highly aggregated.
- **0 pts** — No empirical performance data.

### 3. Equity Applicability (0–2 pts)
- **2 pts** — Tested on US equities (S&P 500, Russell 1000/2000, individual stocks) at daily or weekly frequency.
- **1 pt** — Tested on non-US equities, or on daily data but requires minor adaptation for US stocks.
- **0 pts** — Strategy is inherently non-equity (HFT, crypto-native, options-only, futures-only) with no plausible equity analog.

### 4. Implementation Feasibility (0–2 pts)
- **2 pts** — All required inputs are standard OHLCV data or common technical indicators (RSI, ATR, moving averages, volume). Lookback periods and thresholds are specified.
- **1 pt** — Requires one non-standard but readily available input (e.g. earnings calendar, short interest, sector classifications).
- **0 pts** — Requires expensive alternative data, tick-level data, or proprietary datasets unavailable to a retail algo trader.

### 5. Novelty Bonus (0–1 pt)
- **1 pt** — Strategy is meaningfully distinct from trivial well-known approaches (plain MA crossover, basic RSI threshold, naive buy-and-hold). Has a novel combination, edge, or insight.
- **0 pts** — Essentially a repackaging of a standard textbook strategy with no new insight.

**Score interpretation:**
- **8–10**: Implement immediately — clear rules, strong evidence, equity-native, standard data.
- **6–7**: Good candidate — minor gaps filled during extraction, solid overall.
- **4–5**: Marginal — interesting idea but too vague, theory-heavy, or non-equity to implement directly.
- **0–3**: Discard — no actionable strategy, no equity application, or purely theoretical.

## Output Format

Return a **JSON array** containing **every** input paper with all original fields preserved, plus exactly two new fields added to each object:

- `"score"` — integer from 0 to 10
- `"score_rationale"` — string of 1–3 sentences explaining the score; cite specific criteria met or missed

**Rules:**
- Include ALL papers from the input, even those scoring 0.
- Do not add any fields other than `score` and `score_rationale`.
- Do not remove or rename any existing fields from the input.
- Output only the raw JSON array — no markdown fences, no preamble, no trailing commentary.

**Example of a single scored paper object** (fields truncated for brevity):

```
[
  {
    "title": "Momentum Crashes and Reversal Signals in US Equities",
    "url": "https://arxiv.org/abs/2401.12345",
    "abstract": "We propose a daily momentum strategy ...",
    "score": 8,
    "score_rationale": "Clear 12-1 month momentum entry with RSI confirmation exit, tested on S&P 500 daily data with 1.4 Sharpe out-of-sample 2010-2023. Pure OHLCV inputs, all thresholds specified. Deducted 2pts: novelty is modest (momentum is well-studied) and stop placement requires ATR inference."
  }
]
```

Now output the complete scored JSON array for all papers listed above.
