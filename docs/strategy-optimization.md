# Strategy Optimization Framework

This project should not treat a backtest as proof that a strategy works. The backtest is a filter for obviously bad ideas. A strategy can enter paper trading only after it passes data quality checks, cost/slippage assumptions, stress scenarios, and daily explanation audits.

## Data Layers

Use layered data rather than a single ranking signal:

1. Market data: daily K-lines, realtime quotes, limit-up/down state, suspension state, turnover, amount, market cap, PE.
2. Money flow: individual stock main net inflow amount and ratio, plus sector/concept money flow where available.
3. Fundamentals and quality: profitability, revenue/earnings trend, leverage, cash flow, valuation, and shareholder structure when a reliable point-in-time source is available.
4. News and social sentiment: exchange announcements, company news, policy news, and social media text.
5. Macro and market regime: index trend, market breadth, rates, FX, commodity prices, CPI/PPI/PMI, credit conditions.

Market data, money flow, event factors, macro regime factors, buy-point evaluation, sell-point evaluation, and stress-test diagnostics now have code paths. Fundamentals and point-in-time full-market universe data still need reliable data sources before they can be trusted in research or live simulation.

## NLP Event Factors

DeepSeek or another large language model can help extract structured event factors from text, but it must not directly decide orders.

The target output should be strict JSON, for example:

```json
{
  "symbol": "000001.SZ",
  "event_date": "2026-08-21",
  "event_type": "earnings|policy|litigation|contract|capacity|mna|other",
  "sentiment": -1,
  "impact_score": 0.35,
  "confidence": 0.72,
  "summary": "short factual summary",
  "source_url": "https://..."
}
```

Guardrails:

- Store raw text source, extraction prompt version, model name, and generated JSON.
- Reject records with low confidence, missing source URL, or invalid JSON.
- Use event factors as small scoring adjustments or hard risk flags only.
- Never let generated text place trades without deterministic validation.

Current ingestion command:

```bash
python -m aqt.cli ingest-event-factors --input data/factors/events.jsonl --output data/factors/events.csv
```

## Current Selection Logic

The local paper system now evaluates every symbol in the configured universe and writes `selection_candidates.csv` for each trading day.

Inputs:

- Health gate: ST or delisting risk, suspension, limit-up no-buy, liquidity, PE, float market cap.
- Money-flow gate: recent main net inflow days, total main net inflow, average main net inflow ratio.
- External factors: validated news/event score, macro regime score, hard negative-event or risk-off flags.
- Technical entry: distance from support, distance to target, recent run-up, short trend slope.
- Risk quality: recent volatility, drawdown from 60-day high, momentum.

Decisions:

- `BLOCK`: do not buy; hard risk or missing required data.
- `BUY_WAIT`: worth tracking but not at a qualified buy point or money flow is not confirmed.
- `BUY_READY`: all hard gates pass and buy point is acceptable.

Orders can only be generated from `BUY_READY` rows. The order file keeps the selection rank, score, positive reasons, and blocking/wait reasons.

## Sell Point Logic

Each open position gets an explicit sell-point evaluation:

- `SELL_NOW`: ST/delisting risk, support break with weakening trend, or target reached with fading trend.
- `SELL_WAIT_T1`: sell signal exists but T+1 availability blocks the sale.
- `SELL_WATCH`: money flow outflow plus weak trend, large drawdown from high, or loss with weak trend.
- `HOLD`: price remains above support and target/trend conditions do not require exit.

Daily outputs:

- `position_advice.csv`
- `position_advice.md`
- `sell_points.csv`
- `external_factors.csv` when event or macro factors are enabled

## Backtest Requirements

Backtests must include:

- Fees: commission, minimum commission, stamp tax.
- Slippage: configurable bps model, with stress cases.
- Trading rules: lot size, T+1, no buy on limit-up, suspension no trade.
- Survivorship-bias control: use point-in-time universe membership when available; mark current static universes as biased.
- Stress tests: higher slippage, lower liquidity, delayed execution, gap down, forced exit, market-wide selloff.

The current engine already models fees, slippage, lot size, T+1, limit-price touch checks, limit-up buy blocks, limit-down sell blocks, liquidity-aware partial fills, and order rejections. The stress-test CLI reruns baseline, higher slippage, doubled costs, lower liquidity, tighter volume participation, and market-shock scenarios:

```bash
python -m aqt.cli stress-test --config configs/smallcap.yaml --output runs/stress_test
```

It still does not have a point-in-time full-market universe. Until that exists, backtest results are engineering diagnostics, not evidence of a deployable strategy.

## Next Implementation Steps

1. Add a point-in-time universe builder with listed/delisted dates, ST date ranges, and board filters.
2. Add reliable fundamentals and point-in-time financial statement factors.
3. Connect real news/social/macro data collectors with source provenance, not hand-built placeholder files.
4. Add walk-forward validation: train/select parameters on one period, freeze them, then evaluate only on a later out-of-sample period.
