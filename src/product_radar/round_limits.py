"""How much one programme round may do (Plan R10 W8), from config, not code.

``config/research/round_limits.json`` is the single source of a round's
allowances: ``source_reads`` (the turn policy's ``source_callbacks``, applied by
``round_policy``; ``work_turns.DEFAULT_POLICY`` holds the same numbers as
static defaults, and the run's ``--source-actions`` default;
16 since W8, raised to 20 only when the live check (W13) shows a three-hour
median round of about 12 minutes or less and CPU use under 80%) and
``ai_calls`` (``model_executions``; 10, raised only once W12 has measured each
AI job's share of the weekly allowance). The slow signed-in reads stay bounded
by the signed-in browser's own per-round time share (``signed_in_round``),
whatever the read allowance is.
"""

from copy import deepcopy
from pathlib import Path

from .models import SafetyError
from .storage import load_json

SETTINGS = Path(__file__).resolve().parents[2] / "config/research/round_limits.json"


def validate_round_limits(value):
    """The saved round limits, or ``ROUND_LIMITS_INVALID``."""
    if (not isinstance(value, dict) or set(value) != {"schema_version", "kind", "source_reads", "ai_calls"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] != "programme-round-limits-v1"
            or type(value["source_reads"]) is not int or not 1 <= value["source_reads"] <= 1000
            # A round keeps its writing share and one ordinary call (work_turns).
            or type(value["ai_calls"]) is not int or not 2 <= value["ai_calls"] <= 1000):
        raise SafetyError("ROUND_LIMITS_INVALID")
    return value


def round_limits():
    return validate_round_limits(load_json(SETTINGS, 4096))


def round_policy(source_reads=None):
    """The work-turn policy for one round: the configured read and AI-call
    allowances (``source_reads`` overrides the read allowance)."""
    from .work_turns import DEFAULT_POLICY
    limits = round_limits()
    policy = deepcopy(DEFAULT_POLICY)
    policy["source_callbacks"] = limits["source_reads"] if source_reads is None else source_reads
    policy["model_executions"] = limits["ai_calls"]
    policy["exploration_slots"] = min(policy["exploration_slots"], policy["source_callbacks"])
    return policy
