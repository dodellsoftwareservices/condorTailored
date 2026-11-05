"""US-oriented connector and free-data catalog; contains no credential values.

This is a technical shortlist, not a legal allowlist. Product, state and entity
eligibility must be checked with each venue before use. Public market data and
paper execution are deliberately separate capabilities: a connector that can
read a mainnet book does not thereby provide a simulator.

The environment mappings are an import path into Hummingbot's encrypted
credential store, not a second credential backend. ``alpaca_paper`` is the one
exception: the existing Alpaca paper-only client reads its two variables
directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CredentialLine:
    """One placeholder in ``.env`` and its Hummingbot credential field."""

    variable: str
    field: str = ""


@dataclass(frozen=True)
class Venue:
    id: str
    label: str
    assets: str
    connector: str = ""
    credentials: tuple[CredentialLine, ...] = ()
    probe_pair: str = ""
    data_connectors: tuple[str, ...] = ()
    public_book_url: str = ""
    direct_public: bool = False
    direct_env: bool = False
    simulation: bool = False
    local_paper: bool = False
    data_note: str = ""
    simulation_note: str = "No venue-provided simulator is configured."
    eligibility_note: str = (
        "Confirm US/state and product eligibility before connecting."
    )

    @property
    def storage(self) -> str:
        return (
            "condor/.env (paper client)"
            if self.direct_env
            else "Hummingbot encrypted store"
        )

    @property
    def auth_supported(self) -> bool:
        return bool(self.credentials)


def _line(connector: str, suffix: str, variable: str) -> CredentialLine:
    return CredentialLine(variable, f"{connector}_{suffix}")


def _lighter(connector: str, *, spot: bool) -> tuple[CredentialLine, ...]:
    lines = [_line(connector, "l1_address", f"{connector.upper()}_L1_ADDRESS")]
    if spot:
        lines.append(
            _line(connector, "account_index", f"{connector.upper()}_ACCOUNT_INDEX")
        )
    lines.extend(
        _line(connector, suffix, f"{connector.upper()}_{suffix.upper()}")
        for suffix in ("api_key_index", "api_private_key", "account_limit")
    )
    return tuple(lines)


US_VENUES: tuple[Venue, ...] = (
    Venue(
        id="lighter_perpetual",
        label="Lighter perpetuals — mainnet",
        assets="crypto perpetuals",
        connector="lighter_perpetual",
        credentials=_lighter("lighter_perpetual", spot=False),
        probe_pair="BTC-USDC",
        data_connectors=("lighter_perpetual",),
        data_note="Public mainnet books/candles need no account credential.",
        simulation_note="Use lighter_perpetual_testnet below; mainnet keys are not paper keys.",
    ),
    Venue(
        id="lighter_perpetual_testnet",
        label="Lighter perpetuals — testnet",
        assets="simulated crypto perpetuals",
        connector="lighter_perpetual_testnet",
        credentials=_lighter("lighter_perpetual_testnet", spot=False),
        data_connectors=("lighter_perpetual",),
        simulation=True,
        data_note="Research reads mainnet data, never synthetic testnet prices.",
        simulation_note="Separate Lighter testnet account/key required; no real-money orders.",
    ),
    Venue(
        id="lighter",
        label="Lighter spot — mainnet",
        assets="crypto spot",
        connector="lighter",
        credentials=_lighter("lighter", spot=True),
        probe_pair="ETH-USDC",
        data_connectors=("lighter",),
        data_note="Public mainnet books/candles need no account credential.",
        simulation_note="Use lighter_testnet below; mainnet keys are not paper keys.",
    ),
    Venue(
        id="lighter_testnet",
        label="Lighter spot — testnet",
        assets="simulated crypto spot",
        connector="lighter_testnet",
        credentials=_lighter("lighter_testnet", spot=True),
        data_connectors=("lighter",),
        simulation=True,
        data_note="Research reads mainnet data, never synthetic testnet prices.",
        simulation_note="Separate Lighter testnet account/key required; no real-money orders.",
    ),
    Venue(
        id="coinbase_advanced_trade",
        label="Coinbase Advanced Trade",
        assets="crypto spot",
        connector="coinbase_advanced_trade",
        credentials=(
            _line(
                "coinbase_advanced_trade",
                "api_key",
                "COINBASE_ADVANCED_TRADE_API_KEY",
            ),
            _line(
                "coinbase_advanced_trade",
                "api_secret",
                "COINBASE_ADVANCED_TRADE_API_SECRET",
            ),
        ),
        probe_pair="BTC-USD",
        data_connectors=("coinbase_advanced_trade",),
        direct_public=True,
        public_book_url="https://api.coinbase.com/api/v3/brokerage/market/product_book?product_id=BTC-USD&limit=5",
        data_note="Hummingbot currently hits an authenticated path and returns 401 keyless; use the official public endpoint or fix its adapter.",
        simulation_note="No authenticated sandbox is wired; local paper simulation only.",
        local_paper=True,
    ),
    Venue(
        id="kraken",
        label="Kraken",
        assets="crypto spot",
        connector="kraken",
        credentials=(
            _line("kraken", "api_key", "KRAKEN_API_KEY"),
            _line("kraken", "secret_key", "KRAKEN_API_SECRET"),
        ),
        probe_pair="BTC-USD",
        data_connectors=("kraken",),
        data_note="Public books/candles via Hummingbot; no key required.",
        simulation_note="No venue spot sandbox is wired; local paper simulation only.",
        local_paper=True,
        eligibility_note="US/state product availability differs; derivatives are not included here.",
    ),
    Venue(
        id="gemini",
        label="Gemini",
        assets="crypto spot",
        connector="gemini",
        credentials=(
            _line("gemini", "api_key", "GEMINI_API_KEY"),
            _line("gemini", "api_secret", "GEMINI_API_SECRET"),
        ),
        probe_pair="BTC-USD",
        data_connectors=("gemini",),
        data_note="Public books via Hummingbot; no key required.",
        simulation_note="No venue spot sandbox is wired; local paper simulation only.",
        local_paper=True,
    ),
    Venue(
        id="bitstamp",
        label="Bitstamp",
        assets="crypto spot",
        connector="bitstamp",
        credentials=(
            _line("bitstamp", "api_key", "BITSTAMP_API_KEY"),
            _line("bitstamp", "api_secret", "BITSTAMP_API_SECRET"),
        ),
        probe_pair="BTC-USD",
        data_connectors=("bitstamp",),
        data_note="Public books via Hummingbot; no key required.",
        simulation_note="Bitstamp documents a separate sandbox, but no sandbox connector is wired here.",
        local_paper=True,
    ),
    Venue(
        id="alpaca_paper",
        label="Alpaca — paper",
        assets="US stocks/ETFs; Alpaca crypto data",
        credentials=(
            CredentialLine("ALPACA_API_KEY_ID"),
            CredentialLine("ALPACA_API_SECRET_KEY"),
        ),
        direct_env=True,
        simulation=True,
        data_note="Basic: IEX real-time stock data, indicative options, crypto; plan limits apply.",
        simulation_note="Paper host is pinned by AlpacaPaperClient; use paper-generated keys only.",
    ),
)

VENUES_BY_ID = {venue.id: venue for venue in US_VENUES}
assert len(VENUES_BY_ID) == len(US_VENUES)


def venue_ids() -> tuple[str, ...]:
    return tuple(venue.id for venue in US_VENUES)
