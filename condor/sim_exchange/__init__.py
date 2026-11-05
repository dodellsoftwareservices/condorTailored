"""Simulated Kraken spot exchange: live public prices, realistic fills and costs.

Nothing in this package can place a real order. The only network client is
:mod:`condor.sim_exchange.kraken_public`, which is pinned to Kraken's
unauthenticated ``/0/public/`` endpoints and never sees an API key.
"""
