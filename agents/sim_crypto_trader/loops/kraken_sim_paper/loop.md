---
name: Kraken Sim Paper
description: Every 15 minutes, review the simulated Kraken account and watched
  pairs, and act only when an edge clearly beats round-trip costs.
agent_key: openrouter:nvidia/nemotron-3-ultra-550b-a55b:free
skills: []
default_config:
  agent_key: openrouter:nvidia/nemotron-3-ultra-550b-a55b:free
  connector_name: kraken_sim
  trading_pair: BTC-USD
  frequency_sec: 900
  total_amount_quote: 10000
  leverage: null
  execution_mode: loop
  risk_limits:
    max_position_size_quote: 5000
    max_open_executors: 1
default_trading_context: ''
created_by: 0
created_at: '2026-11-03T18:50:39.000000+00:00'
---

# Kraken Sim Paper: Tick Instructions

Follow the Sim Crypto Trader brain exactly. This loop trades the **simulated** Kraken
account only, through the `sim_*` tools. There is no real account, key or bot.

## Fixed envelope

- **Tools override.** The framework rules above about `create_*_executor`, Hummingbot and
  bot deployment do **not** apply to this loop. Your only trading tool is `sim_submit_order`,
  and your only market data is `sim_market_snapshot`. Never look for a Hummingbot server.
- In a run-once or dry-run experiment there is no journal: put the entry you would have
  written at the end of your response instead of calling the journal tools.
- Watched pairs: `BTC-USD`, `ETH-USD`, `SOL-USD` (USD-quoted spot only).
- Run at the shipped `frequency_sec: 900`. Edge trades are rare, so most ticks hold.
  Never trade to calibrate execution or generate fills. Every new position must
  clear the brain's cost-aware edge test and have a written exit plan.
- The simulated account, its starting balance and every limit are owned by the user. If an
  order is rejected for a limit, accept it; do not look for a way around it.
- Record a baseline in the journal on your first tick: the BTC-USD mid price, so the
  buy-and-hold comparison in KR1 can be computed later.
- Each tick ends with one journal entry, whether or not you traded.
