"""A stock Agent's ``tools:`` allowlist binds on every backend, so it must name
what the agent is actually told to call.

It used to bind only pydantic-ai seats. On a Claude seat it was decoration, and it
drifted unnoticed: four of the six stock lists omitted tools their own playbooks
call — ``solana_dex_lp_expert``'s shutdown notifies the owner with a tool its list never named. Now
the spawner never mounts what a list leaves out (``toolsets.seat_mutes``), so a
missing name is a step the agent cannot take. These pin every stock list against
the two sources that say what an agent needs.
"""

import re
from pathlib import Path

import pytest
import yaml

from mcp_servers.condor import profiles as condor_profiles
from mcp_servers.hummingbot_api import profiles as hummingbot_profiles

AGENTS = Path(__file__).resolve().parent.parent / "agents"

#: What an attended specialist can mount at all.
AGENT_RING = set(condor_profiles.PROFILE_TOOLS["agent"]) | set(
    hummingbot_profiles.PROFILE_TOOLS["agent"]
)

#: What the framework skills every agent inherits (``agent_framework``,
#: ``loop_builder``, ``operate_your_loop``, ``skill_authoring``,
#: ``self_improve``) and the tick's house prompt tell *any* agent to call.
FRAMEWORK = {
    "delegate",
    "send_notification",
    "run_code",
    "manage_memory",
    "manage_skill",
    "manage_routines",
    "manage_agent_controllers",
    "trading_agent_journal_read",
    "trading_agent_journal_write",
    "manage_agents",
    "manage_loops",
    "control_agent",
    "get_available_models",
}

#: Tool names an agent's own files mention without calling them.
#: Tools an agent's own files NAME without calling — a mention this scan would
#: otherwise read as a missing allowlist entry. Kept narrow and per agent: the
#: common case is a playbook naming a tool to say the seat does NOT have it,
#: which is the opposite of needing it allowlisted.
NOT_CALLS: dict[str, set[str]] = {
    # shutdown.md names execute_swap to rule it out: the winddown cannot swap
    # leftover tokens back, and saying so by name is what stops the agent
    # spending its cleanup pass discovering that (LP-5).
    "solana_dex_lp_expert": {"execute_swap"},
}


#: Seats sealed on purpose: they hold NO code execution, delegation, routine, agent-management
#: or credential-adjacent tools, so the "framework family" every other agent carries is
#: withheld by design. ``run_code`` runs Python inside the main process, where the Hummingbot
#: credential store and the Alpaca keys are reachable; handing it to a simulated-trading agent
#: would quietly break its promise that no real account can be touched. Each sealed agent is
#: held to ``SEALED_FORBIDDEN`` below instead of the family rule.
SEALED = {"sim_crypto_trader", "sim_futures_trader"}
SEALED_FORBIDDEN = {
    "run_code",
    "delegate",
    "manage_routines",
    "manage_agent_controllers",
    "manage_agents",
    "manage_loops",
    "control_agent",
    "manage_servers",
    "manage_skill",
    "manage_memory",
    "send_notification",
    "get_available_models",
}


def _frontmatter(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split("---")[1]) or {}


def _allowlisted_agents() -> list[str]:
    return sorted(
        path.parent.name
        for path in AGENTS.glob("*/AGENT.md")
        if _frontmatter(path).get("tools")
    )


def _allowlist(slug: str) -> set[str]:
    return set(_frontmatter(AGENTS / slug / "AGENT.md")["tools"])


def _named_in_own_files(slug: str) -> dict[str, list[str]]:
    """``{tool: [file, ...]}`` for every ring tool the agent's own markdown names."""
    home = AGENTS / slug
    found: dict[str, list[str]] = {}
    for path in sorted(home.rglob("*.md")):
        text = path.read_text()
        for tool in AGENT_RING:
            if re.search(rf"\b{tool}\b", text):
                found.setdefault(tool, []).append(str(path.relative_to(home)))
    return found


ALLOWLISTED = _allowlisted_agents()


def test_there_are_lists_to_check():
    assert ALLOWLISTED, "no stock agent names an allowlist any more"


@pytest.mark.parametrize("slug", ALLOWLISTED)
def test_the_list_names_only_tools_the_seat_can_mount(slug):
    """A typo, or a tool no ring mounts any more, is a name that grants nothing."""
    assert _allowlist(slug) <= AGENT_RING, sorted(_allowlist(slug) - AGENT_RING)


@pytest.mark.parametrize("slug", sorted(set(ALLOWLISTED) - SEALED))
def test_the_list_carries_the_framework_family(slug):
    missing = FRAMEWORK - _allowlist(slug)
    assert not missing, f"{slug} inherits playbooks that call {sorted(missing)}"


@pytest.mark.parametrize("slug", ALLOWLISTED)
def test_the_list_covers_what_the_agents_own_files_call(slug):
    exempt = NOT_CALLS.get(slug, set())
    missing = {
        tool: files
        for tool, files in _named_in_own_files(slug).items()
        if tool not in _allowlist(slug) and tool not in exempt
    }
    assert not missing, f"{slug}'s own files call tools its list leaves out: {missing}"


def test_every_exemption_is_still_needed():
    """An exemption whose mention is gone would hide the next real one."""
    for slug, exempt in NOT_CALLS.items():
        named = _named_in_own_files(slug)
        stale = {t for t in exempt if t not in named or t in _allowlist(slug)}
        assert not stale, f"drop {sorted(stale)} from NOT_CALLS[{slug!r}]"


@pytest.mark.parametrize("slug", sorted(SEALED))
def test_a_sealed_seat_holds_no_tool_that_could_reach_a_real_account(slug):
    leaked = _allowlist(slug) & SEALED_FORBIDDEN
    assert not leaked, f"{slug} is sealed but its list names {sorted(leaked)}"
    # Every Hummingbot trading tool is also out: a sealed simulator must not be able to trade for real.
    assert not (_allowlist(slug) & set(hummingbot_profiles.PROFILE_TOOLS["full"])), slug


def test_every_sealed_agent_exists_and_is_allowlisted():
    assert SEALED <= set(ALLOWLISTED), sorted(SEALED - set(ALLOWLISTED))
