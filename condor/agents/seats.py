"""Which model, fallback and tools each seat actually runs with.

A model is chosen in five places: the chat default (``CONDOR_DEFAULT_AGENT``, then
Condor's ``AGENT.md``), each agent's own ``agent_key``, a loop's run pin, the shift
runner (profile, then ``agent-config.json``), and the fallback a delegated run takes
when its backend is down. Setting one of them says nothing about the others -- the
``model`` in ``agent-config.json`` reaches only shifts -- so a run can be on a
different provider than whoever configured it believes.

This module resolves each place with the same functions the runtime uses and lays
the answers side by side. It reads and decides nothing: changing a seat's model
means changing the place it comes from.

    uv run python -m condor.agents.seats
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from condor import agent_config
from condor.agents.agent import Agent, AgentStore
from condor.agents.agent_run import fallback_agent_key
from condor.llm import options
from condor.memory.paths import CHAT_SLUG
from condor.runtime import danger
from mcp_servers.condor.profiles import canonical_tool_name

log = logging.getLogger(__name__)

#: ``manage_bots`` is gated per action, not by name, so it is not in
#: ``DANGEROUS_TOOLS`` -- but a seat that can call it can deploy.
_MUTATING_BY_NAME = frozenset(danger.DANGEROUS_TOOLS) | {"manage_bots"}


def _short_name(tool: str) -> str:
    """The bare name an allowlist entry matches on (``mcp__condor__x`` -> ``x``)."""
    return canonical_tool_name(tool).rsplit("__", 1)[-1]


@dataclass(frozen=True)
class Seat:
    name: str
    kind: str  # "chat" | "agent" | "shift"
    model: str  # "" when nothing names one
    source: str
    #: ``None`` is unrestricted (the allowlist is empty); otherwise the tool names.
    tools: tuple[str, ...] | None = None

    @property
    def unrestricted(self) -> bool:
        return self.tools is None

    @property
    def mutating_tools(self) -> tuple[str, ...]:
        """Allowlisted tools that can change an account, or ``()``.

        An unrestricted seat has no list to inspect and can reach all of them;
        callers check :attr:`unrestricted` first.
        """
        return tuple(t for t in self.tools or () if _short_name(t) in _MUTATING_BY_NAME)

    @property
    def read_only(self) -> bool:
        return not self.unrestricted and not self.mutating_tools


def _agent_seat(agent: Agent) -> Seat:
    if agent.agent_key:
        model, source = agent.agent_key, f"agents/{agent.slug}/AGENT.md"
    else:
        model, source = (
            options.DEFAULT_AGENT,
            "chat default (AGENT.md has no agent_key)",
        )
    return Seat(
        name=agent.slug,
        kind="agent",
        model=model,
        source=source,
        tools=tuple(agent.tools) if agent.tools else None,
    )


def resolve_seats(store: AgentStore | None = None) -> list[Seat]:
    """Every seat, chat first, then the shift runner, then each agent by slug."""
    store = store or AgentStore()
    chat = store.get(CHAT_SLUG)
    seats = [
        Seat(
            name="chat",
            kind="chat",
            model=options.DEFAULT_AGENT,
            source="CONDOR_DEFAULT_AGENT / agents/condor/AGENT.md",
            tools=tuple(chat.tools) if chat and chat.tools else None,
        )
    ]

    cfg = agent_config.get_agent_config()
    seats.append(
        Seat(
            name="shift",
            kind="shift",
            model=cfg.model or "",
            source=(
                "agent-config.json"
                if cfg.model
                else "nowhere (a profile must name one)"
            ),
        )
    )

    seats.extend(
        _agent_seat(a)
        for a in sorted(store.list_all(), key=lambda a: a.slug)
        if a.slug != CHAT_SLUG
    )
    return seats


def render(seats: list[Seat]) -> str:
    """The table printed at startup and by ``python -m condor.agents.seats``."""
    fallback = fallback_agent_key()
    rows = []
    for seat in seats:
        if seat.kind == "shift":
            tools = "-"
        elif seat.unrestricted:
            tools = "UNRESTRICTED"
        elif seat.mutating_tools:
            tools = "can mutate: " + ", ".join(seat.mutating_tools)
        else:
            tools = f"read-only ({len(seat.tools or ())})"
        rows.append((seat.name, seat.model or "NONE", tools))
    name_w = max([len("seat"), *(len(r[0]) for r in rows)])
    model_w = max([len("model"), *(len(r[1]) for r in rows)])
    lines = [
        f"{'seat':<{name_w}}  {'model':<{model_w}}  tools",
        f"{'-' * name_w}  {'-' * model_w}  -----",
    ]
    lines.extend(f"{n:<{name_w}}  {m:<{model_w}}  {t}" for n, m, t in rows)
    lines.append("")
    lines.append(
        f"fallback when a backend is down: {fallback}"
        if fallback
        else "fallback when a backend is down: none (the run fails)"
    )
    lines.append(
        "a chat user's own /agent choice and a loop's run pin override the rows above"
    )
    return "\n".join(lines)


def log_seats() -> None:
    """Log the table once, for the process log. Never raises: it is a report."""
    try:
        log.info("Resolved model seats:\n%s", render(resolve_seats()))
    except Exception:  # noqa: BLE001 - a broken report must not stop the bot
        log.exception("Could not resolve model seats")


if __name__ == "__main__":
    print(render(resolve_seats()))
