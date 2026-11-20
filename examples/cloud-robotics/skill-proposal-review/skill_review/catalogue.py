"""Closed catalogue of typed, allowlisted SIMULATED skills.

SIMULATION ONLY. Nothing in this module executes anything: a ``SkillSpec`` is
data describing what a proposal may ask for. There is no executor in this
package, no ROS 2 binding, no shell and no network access.

Design rules enforced by ``validate_catalogue`` (run at import time and in the
tests):

* every skill and argument name is a lowercase ASCII identifier;
* argument kinds are a closed set (``enum``, ``number``, ``integer``,
  ``task_id``); there is deliberately no free-form ``string``, ``path``,
  ``url``, ``command`` or ``topic`` kind;
* names that suggest raw actuation or code execution are refused;
* every precondition id is one the reviewer knows how to evaluate.
"""

import re
from dataclasses import dataclass
from types import MappingProxyType

CATALOGUE_VERSION = "skill-catalogue/v1"
PROPOSAL_VERSION = "skill-proposal/v1"

# Simulated robots this lab accepts proposals for. Anything else is rejected.
SUPPORTED_ROBOTS = ("sim_robot_1",)
ROBOT_STATES = ("idle", "moving", "benchmark_running")
# Position/state older than this is "stale" for consequential skills.
MAX_STATE_AGE_MS = 2000

# Closed value sets. SYNTHETIC names: they do not refer to real map data or to
# benchmark scripts that exist in this repository.
WAYPOINTS = ("dock_a", "station_1", "station_2", "charging_bay")
BENCHMARKS = ("sim_smoke_v1", "sim_nav_basic_v1")
SCENE_VIEWS = ("summary", "objects", "obstacles")

SAFETY_READ_ONLY = "read_only"
SAFETY_STOP_ONLY = "stop_only"
SAFETY_CONSEQUENTIAL = "consequential"
SAFETY_LEVELS = (SAFETY_READ_ONLY, SAFETY_STOP_ONLY, SAFETY_CONSEQUENTIAL)

ARG_KINDS = ("enum", "number", "integer", "task_id")

# Preconditions the reviewer can evaluate (see reviewer._PRECONDITIONS).
PRECONDITION_IDS = ("state_fresh", "robot_idle", "no_estop", "active_task_matches")

IDENT_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
TASK_ID_RE = re.compile(r"task-[0-9]{1,6}")

# Substrings that must never appear in a skill or argument name. This is a
# self-check on the catalogue author, not a filter on proposals.
FORBIDDEN_NAME_PARTS = (
    "cmd_vel",
    "shell",
    "exec",
    "eval",
    "raw",
    "publish",
    "topic",
    "kubectl",
    "sudo",
    "bash",
    "system",
    "url",
    "path",
    "file",
    "command",
)


@dataclass(frozen=True)
class ArgSpec:
    name: str
    kind: str
    required: bool = True
    unit: str = ""
    allowed: tuple = ()
    minimum: float = 0.0
    maximum: float = 0.0
    doc: str = ""


@dataclass(frozen=True)
class SkillSpec:
    name: str
    summary: str
    safety_level: str
    args: tuple = ()
    preconditions: tuple = ()


def _build():
    skills = (
        SkillSpec(
            name="get_status",
            summary="Report the simulated robot's status. No arguments.",
            safety_level=SAFETY_READ_ONLY,
        ),
        SkillSpec(
            name="inspect_scene",
            summary="Return a read-only description of the simulated scene.",
            safety_level=SAFETY_READ_ONLY,
            args=(
                ArgSpec("view", "enum", allowed=SCENE_VIEWS, doc="which view of the scene"),
            ),
        ),
        SkillSpec(
            name="hold_position_sim",
            summary="Stop-type request: hold the simulated robot where it is.",
            safety_level=SAFETY_STOP_ONLY,
        ),
        SkillSpec(
            name="cancel_simulated_task",
            summary="Stop-type request: cancel the currently active simulated task.",
            safety_level=SAFETY_STOP_ONLY,
            args=(ArgSpec("task_id", "task_id", doc="id of the active task, task-<1..6 digits>"),),
            preconditions=("active_task_matches",),
        ),
        SkillSpec(
            name="navigate_to_named_waypoint_sim",
            summary="Ask the simulator to drive to a NAMED waypoint (never coordinates or velocities).",
            safety_level=SAFETY_CONSEQUENTIAL,
            args=(
                ArgSpec("waypoint", "enum", allowed=WAYPOINTS, doc="named waypoint"),
                ArgSpec(
                    "speed_limit_mps",
                    "number",
                    required=False,
                    unit="m/s",
                    minimum=0.05,
                    maximum=0.5,
                    doc="upper speed bound for the simulated move",
                ),
            ),
            preconditions=("state_fresh", "robot_idle", "no_estop"),
        ),
        SkillSpec(
            name="start_approved_benchmark",
            summary="Start one of a closed list of pre-approved simulated benchmark ids.",
            safety_level=SAFETY_CONSEQUENTIAL,
            args=(
                ArgSpec("benchmark_id", "enum", allowed=BENCHMARKS, doc="approved benchmark id"),
                ArgSpec(
                    "max_runtime_s",
                    "integer",
                    required=False,
                    unit="s",
                    minimum=1,
                    maximum=300,
                    doc="wall-clock budget for the simulated run",
                ),
            ),
            preconditions=("state_fresh", "robot_idle", "no_estop"),
        ),
    )
    return MappingProxyType({s.name: s for s in skills})


def validate_catalogue(catalogue):
    """Raise ValueError if ``catalogue`` violates the design rules above."""
    for key, spec in catalogue.items():
        if key != spec.name:
            raise ValueError(f"catalogue key {key!r} != spec name {spec.name!r}")
        names = [spec.name] + [a.name for a in spec.args]
        for n in names:
            if not isinstance(n, str) or IDENT_RE.fullmatch(n) is None:
                raise ValueError(f"bad identifier {n!r}")
            for part in FORBIDDEN_NAME_PARTS:
                if part in n:
                    raise ValueError(f"name {n!r} contains forbidden part {part!r}")
        if spec.safety_level not in SAFETY_LEVELS:
            raise ValueError(f"{spec.name}: unknown safety level {spec.safety_level!r}")
        seen = set()
        for a in spec.args:
            if a.name in seen:
                raise ValueError(f"{spec.name}: duplicate argument {a.name!r}")
            seen.add(a.name)
            if a.kind not in ARG_KINDS:
                raise ValueError(f"{spec.name}.{a.name}: unknown kind {a.kind!r}")
            if a.kind == "enum":
                if not a.allowed:
                    raise ValueError(f"{spec.name}.{a.name}: empty enum")
                for v in a.allowed:
                    if not isinstance(v, str) or IDENT_RE.fullmatch(v) is None:
                        raise ValueError(f"{spec.name}.{a.name}: bad enum value {v!r}")
            if a.kind in ("number", "integer") and not a.minimum < a.maximum:
                raise ValueError(f"{spec.name}.{a.name}: empty numeric range")
            if a.kind in ("number", "integer") and not a.unit:
                raise ValueError(f"{spec.name}.{a.name}: numeric argument needs a unit")
        for p in spec.preconditions:
            if p not in PRECONDITION_IDS:
                raise ValueError(f"{spec.name}: unknown precondition {p!r}")


CATALOGUE = _build()
validate_catalogue(CATALOGUE)


def describe(catalogue=CATALOGUE):
    """Human-readable catalogue listing (deterministic order)."""
    lines = []
    for name in sorted(catalogue):
        spec = catalogue[name]
        lines.append(f"{name}  [{spec.safety_level}]  {spec.summary}")
        for a in spec.args:
            if a.kind == "enum":
                typ = "one of " + "|".join(a.allowed)
            elif a.kind in ("number", "integer"):
                typ = f"{a.kind} {a.minimum:g}..{a.maximum:g} {a.unit}"
            else:
                typ = "id matching task-[0-9]{1,6}"
            req = "required" if a.required else "optional"
            lines.append(f"    arg {a.name}: {typ} ({req})")
        if spec.preconditions:
            lines.append("    preconditions: " + ", ".join(spec.preconditions))
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
