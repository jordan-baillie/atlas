---
id: H1
title: RSI period transfer test
status: proposed
source: agent
created: "2026-03-10"
tags:
  - hypothesis
  - status/proposed
---

# H1: RSI period transfer test

> **Status:** `proposed` | **Source:** agent | **Created:** 2026-03-10

## Hypothesis

RSI(2) works for ConnorsRSI2, does it also work for Williams %R?

## Test Plan

Run williams_percent_r with wr_period=2 instead of 14

## Expected Outcome

Sharpe > 0.3 with wr_period=2

## Related Experiments

_None_

## Related Strategies

- [[connors_rsi2]]
- [[williams_percent_r]]
