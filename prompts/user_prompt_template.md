Review the live trading context below and decide whether the bot should BUY, SELL, or HOLD.

Use only the data provided in this prompt. Do not invent missing prices, news, fundamentals, account values, positions, or prior trades.

Tasks:
1. Review the account and portfolio context.
2. Review current positions.
3. Review the watchlist.
4. Review recent market data.
5. Apply the supplied risk rules.
6. Decide whether to BUY, SELL, or HOLD.
7. Return valid JSON only.

Current datetime:
{{current_datetime}}

Market status:
{{market_status}}

Account summary:
{{account_summary}}

Current positions:
{{positions}}

Watchlist:
{{watchlist}}

Recent market data:
{{recent_market_data}}

Risk rules:
{{risk_rules}}

Previous trades or recent decisions:
{{previous_trades}}

Decision rules:
- Use HOLD if data is insufficient.
- Use HOLD if no trade meets the supplied risk rules.
- symbol is required for BUY, SELL, and HOLD.
- symbol must always be one of the supplied watchlist symbols.
- Do not use "CASH" or "NONE" as symbol values.
- For HOLD, if no single symbol is relevant, choose the broadest supplied market symbol, usually SPY if present.
- If action is HOLD, suggested_allocation_percent must be 0.
- confidence is required for BUY, SELL, and HOLD.
- confidence must always be a number between 0 and 1.
- confidence must never be null.
- For HOLD, confidence represents confidence in the HOLD decision.
- If uncertain, still return a low numeric confidence such as 0.3, not null.
- For BUY or SELL decisions, stop_loss_percent and take_profit_percent may be positive numbers.
- For HOLD decisions, stop_loss_percent must be null.
- For HOLD decisions, take_profit_percent must be null.
- Do not use 0 for stop_loss_percent or take_profit_percent.
- Never suggest a trade outside the supplied watchlist.
- Never suggest a trade outside regular US market hours.
- Do not exceed the supplied max allocation limits.
- Return JSON only.
- Do not include Markdown outside the JSON response.
- Do not include comments in the JSON.

Required output format:
{
  "symbol": "AAPL",
  "action": "BUY",
  "confidence": 0.72,
  "suggested_allocation_percent": 5,
  "reason": "Concise reason for the decision.",
  "stop_loss_percent": 3,
  "take_profit_percent": 6
}

For HOLD responses, keep the same JSON keys but set:
{
  "symbol": "SPY",
  "action": "HOLD",
  "confidence": 0.3,
  "suggested_allocation_percent": 0,
  "reason": "Concise reason for holding.",
  "stop_loss_percent": null,
  "take_profit_percent": null
}
