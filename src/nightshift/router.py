"""The router: which model each call goes to.

Every agent has a ladder of models in its config: a first choice, then the models to move to.
For each call the router orders that ladder by what the caller knows:

    level   start higher up the ladder: a large task, or a retry after the first model did not
            manage (the Coder's escalation, the Setup agent after failed attempts)
    avoid   a model family to put last: the Reviewer avoids the Coder's vendor, so a second family
            checks the work

and, with `agent` given, by what that agent's past outcomes show (outcomes.adjust: models whose
calls keep failing go last, a model that clearly succeeds more often goes first). It drops models
the catalog says cannot do the job (no tool calls for a tool agent, no structured
output for a structured one, or no longer on OpenRouter). The gateway tries the result in order,
moving to the next model when one errors, refuses or returns unusable output.

The ladders are chosen by hand, informed by OpenRouter's benchmark scores and prices (the newest
models often have no coding or agentic score yet, so scores alone cannot rank them).
"""

from __future__ import annotations

from dataclasses import dataclass

from nightshift import catalog, outcomes
from nightshift.catalog import ModelConfigError, ModelInfo
from nightshift.config import AgentModel, get_settings

# Agent name (as the ledger records it) -> its settings field, and whether it calls tools (else it
# returns structured output).
AGENTS: dict[str, tuple[str, bool]] = {
    "relevance_analyst": ("relevance", False),
    "pr_assessor": ("pr_assessor", False),
    "verifier": ("verifier", False),
    "opportunity_scout": ("scout", False),
    "repo_briefer": ("briefer", False),
    "chat": ("chat", True),
    "cartographer": ("cartographer", False),
    "setup": ("setup", False),
    "planner": ("planner", False),
    "coder": ("coder", True),
    "reviewer": ("reviewer", False),
    "pr_writer": ("pr_writer", False),
}


async def agent_routes(agent: str) -> list[Route]:
    """The order the router would use for this agent's next call now, with no task hints."""
    field, tools = AGENTS[agent]
    return await routes(getattr(get_settings(), field), tools=tools, structured=not tools, agent=agent)


@dataclass(frozen=True)
class RouteHint:
    level: int = 0
    avoid: str | None = None  # a model family, e.g. "anthropic"


@dataclass(frozen=True)
class Route:
    info: ModelInfo
    why: str


def family(model: str) -> str:
    """The vendor part of an OpenRouter id: anthropic/claude-sonnet-5.5 -> anthropic."""
    return model.split("/", 1)[0]


async def routes(
    cfg: AgentModel,
    hint: RouteHint | None = None,
    *,
    tools: bool = False,
    structured: bool = False,
    agent: str | None = None,
) -> list[Route]:
    """The models to try for one call, best first, each with the reason it is in that place."""
    hint = hint or RouteHint()
    ladder = cfg.models
    start = max(0, min(hint.level, len(ladder) - 1))
    order = [(m, "first choice" if i == 0 else f"ladder step {i}") for i, m in enumerate(ladder)]
    if agent and get_settings().router_learning:
        order = outcomes.adjust(order, await outcomes.stats(agent))
    # From the starting rung upwards, then back down: a weaker model still beats no answer.
    order = order[start:] + order[:start][::-1]
    if start:
        order[0] = (order[0][0], f"escalated to ladder step {start}")
    if hint.avoid:
        other = [(m, f"{why}, not {hint.avoid} like the code it checks") for m, why in order if family(m) != hint.avoid]
        order = other + [(m, why) for m, why in order if family(m) == hint.avoid]

    out: list[Route] = []
    skipped: list[str] = []
    for model, why in order:
        info = await catalog.model_info(model)
        if info is None:
            skipped.append(f"{model} is not on OpenRouter")
        elif tools and not info.tools:
            skipped.append(f"{model} cannot call tools")
        elif structured and not info.structured:
            skipped.append(f"{model} cannot return structured output")
        else:
            out.append(Route(info, why if not out else f"fallback ({why})"))
    if not out:
        raise ModelConfigError("No usable model for this agent: " + "; ".join(skipped))
    return out
