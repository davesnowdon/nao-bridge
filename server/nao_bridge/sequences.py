from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .contract import SCHEMAS, validate
from .errors import BridgeError

if TYPE_CHECKING:
    from .core import Core, FiniteCommand, Operation, Session

LOG = logging.getLogger(__name__)

MAX_DEPTH = 8
MAX_ACTIONS = 32
MAX_DURATION_SECONDS = 600
MAX_PARALLEL_COMMANDS = 4
COMPOSITES = {"sequence", "parallel"}
ACTION_SCHEMAS = {
    "say": "SpeechCreate",
    "lights": "LightTransitionCreate",
    "gesture": "GestureRunCreate",
    "behavior": "BehaviorRunCreate",
    "autonomy": "AutonomyTransitionCreate",
    "wake": "MotionTransitionCreate",
    "rest": "MotionTransitionCreate",
    "stiffness": "StiffnessCreate",
    "posture": "PostureCreate",
    "move": "MoveCreate",
    "joints": "JointMoveCreate",
    "trajectory": "TrajectoryRunCreate",
}
REQUIRES = {
    "say": "speech.utterances",
    "lights": "lights.transitions",
    "gesture": "motion.gestures",
    "behavior": "behaviors.runs",
    "autonomy": "autonomy.transitions",
    "wake": "motion.wake_rest",
    "rest": "motion.wake_rest",
    "stiffness": "motion.stiffness",
    "posture": "motion.posture_changes",
    "move": "motion.moves",
    "joints": "motion",
    "trajectory": "motion",
}
MOTION_ACTIONS = {
    "wake": "wake",
    "rest": "rest",
    "stiffness": "stiffness",
    "posture": "posture",
    "move": "walk",
}
SEQUENCE_KIND = "sequence.run"
DELAY_KIND = "sequence.delay"


@dataclass
class Step:
    path: tuple[int, ...]
    node: dict[str, Any]
    command: FiniteCommand | None
    start_seconds: float | None
    duration_seconds: float | None
    required_feature: str | None

    def wire(self) -> dict[str, Any]:
        return {
            "path": list(self.path),
            "type": self.node["type"],
            "kind": self.command.kind
            if self.command
            else (DELAY_KIND if self.node["type"] == "delay" else None),
            "resources": sorted(self.command.resources) if self.command else [],
            "start_seconds": self.start_seconds,
            "duration_seconds": self.duration_seconds,
            "required_feature": self.required_feature,
        }


@dataclass
class Schedule:
    resources: set[str]
    exact_duration: float | None
    minimum_duration: float
    native_width: int


@dataclass
class SequencePlan:
    document: dict[str, Any]
    steps: list[Step]
    schedule: Schedule
    diagnostics: list[dict[str, Any]]
    generation: int

    def wire(self) -> dict[str, Any]:
        return {
            "valid": not self.diagnostics,
            "normalized": self.document,
            "connection_generation": self.generation,
            "resources": sorted(self.schedule.resources),
            "required_features": sorted(
                {step.required_feature for step in self.steps if step.required_feature}
            ),
            "estimated_duration_seconds": self.schedule.exact_duration,
            "maximum_parallel_commands": self.schedule.native_width,
            "timeline": [step.wire() for step in self.steps],
            "diagnostics": self.diagnostics,
        }


def sequence_schema() -> dict[str, Any]:
    definitions: dict[str, Any] = {}

    def collect(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
        if isinstance(value, dict):
            ref = value.get("$ref")
            if ref and ref.startswith("#/$defs/"):
                name = ref.rsplit("/", 1)[-1]
                if name not in definitions:
                    definitions[name] = SCHEMAS["$defs"][name]
                    collect(definitions[name])
            for item in value.values():
                collect(item)

    root = {"$ref": "#/$defs/SequenceDocument"}
    collect(root)
    return {"$schema": SCHEMAS["$schema"], **root, "$defs": definitions}


def bound_tree(document: Any) -> None:
    if not isinstance(document, dict):
        raise BridgeError("invalid_request", "A sequence document must be an object")
    # Bound traversal before the recursive JSON Schema validator sees the tree.
    pending = [(document.get("root"), 1)]
    count = 0
    while pending:
        node, depth = pending.pop()
        if not isinstance(node, dict):
            raise BridgeError("invalid_request", "Each sequence step must be an object")
        count += 1
        if depth > MAX_DEPTH or count + len(pending) > 2 * MAX_ACTIONS:
            raise BridgeError(
                "limit_exceeded", "Sequence structure exceeds its node or nesting limit", 422
            )
        if isinstance(node.get("type"), str) and node["type"] in COMPOSITES:
            children = node.get("steps")
            if not isinstance(children, list) or not 1 <= len(children) <= MAX_ACTIONS:
                raise BridgeError("invalid_request", "A sequence group needs 1 to 32 child steps")
            pending.extend((child, depth + 1) for child in children)


def prepare(core: Core, kind: str, arguments: dict[str, Any], *, ready: bool) -> FiniteCommand:
    if kind in MOTION_ACTIONS:
        return core.prepare_motion_action(MOTION_ACTIONS[kind], arguments, ready=ready)
    if kind == "joints":
        return core.prepare_trajectory(
            core.joint_trajectory(arguments), "motion.joint_move", ready=ready
        )
    if kind == "trajectory":
        return core.prepare_trajectory(arguments["trajectory"], ready=ready)
    if kind == "say":
        return core.prepare_utterance(arguments, ready=ready)
    if kind == "gesture":
        return core.prepare_gesture(arguments, ready=ready)
    if kind == "behavior":
        return core.prepare_behavior(arguments, ready=ready)
    if kind == "lights":
        return core.prepare_light_transition(arguments)
    return core.prepare_autonomy(arguments)


async def installed_names(core: Core, names: set[str]) -> set[str]:
    remaining = set(names)
    cursor = None
    # The connection catalog contains at most 512 entries.
    for _ in range(43):
        if not remaining:
            break
        page = await core.behavior_page(cursor, 12)
        remaining.difference_update(item["name"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    return names - remaining


async def plan_sequence(core: Core, document: Any) -> SequencePlan:
    bound_tree(document)
    validate("SequenceDocument", document)
    normalized = copy.deepcopy(document)
    steps: list[Step] = []
    diagnostics: list[dict[str, Any]] = []
    generation = core.generation

    def fail(path: tuple[int, ...], error: BridgeError) -> None:
        diagnostics.append({"path": list(path), **error.payload()})

    def visit(node: dict[str, Any], path: tuple[int, ...], offset: float | None) -> Schedule:
        kind = node["type"]
        if kind not in COMPOSITES:
            if len(steps) >= MAX_ACTIONS:
                raise BridgeError(
                    "limit_exceeded", "A sequence can contain at most 32 actions", 422
                )
            if kind == "delay":
                duration = node["duration"]
                steps.append(Step(path, node, None, offset, duration, None))
                return Schedule(set(), duration, duration, 0)
            feature = REQUIRES[kind]
            arguments = node["arguments"]
            if kind == "say" and arguments.get("animated"):
                feature = "speech.animated_utterances"
            schema = SCHEMAS["$defs"][ACTION_SCHEMAS[kind]]
            for name, prop in schema.get("properties", {}).items():
                if "default" in prop:
                    arguments.setdefault(name, prop["default"])
            command = None
            try:
                command = prepare(core, kind, arguments, ready=False)
                if not command.cancellable:
                    fail(
                        path,
                        BridgeError(
                            "unsupported", "Sequence actions must support cancellation", 422
                        ),
                    )
                if kind == "move":
                    arguments.setdefault("speed", command.payload["move"]["speed"])
                elif kind == "gesture" and arguments["name"] == "wave":
                    arguments.setdefault("side", "right")
                elif kind == "say":
                    for key in ("language", "voice"):
                        if command.payload[key] is not None:
                            arguments.setdefault(key, command.payload[key])
            except BridgeError as exc:
                fail(path, exc)
            duration = command.duration_seconds if command else None
            steps.append(Step(path, node, command, offset, duration, feature))
            return Schedule(
                set(command.resources) if command else set(), duration, duration or 0, 1
            )
        serial = kind == "sequence"
        total = Schedule(set(), 0.0, 0.0, 0)
        for index, child in enumerate(node["steps"]):
            child_offset = offset
            if serial:
                child_offset = (
                    None
                    if offset is None or total.exact_duration is None
                    else offset + total.exact_duration
                )
            segment = visit(child, (*path, index), child_offset)
            conflicts = total.resources & segment.resources
            if not serial and conflicts:
                fail(
                    (*path, index),
                    BridgeError(
                        "resource_conflict",
                        "Parallel branches need disjoint resources",
                        422,
                        {"resources": sorted(conflicts)},
                    ),
                )
            total.resources.update(segment.resources)
            if total.exact_duration is None or segment.exact_duration is None:
                total.exact_duration = None
            elif serial:
                total.exact_duration += segment.exact_duration
            else:
                total.exact_duration = max(total.exact_duration, segment.exact_duration)
            if serial:
                total.minimum_duration += segment.minimum_duration
                total.native_width = max(total.native_width, segment.native_width)
            else:
                total.minimum_duration = max(total.minimum_duration, segment.minimum_duration)
                total.native_width += segment.native_width
        return total

    schedule = visit(normalized["root"], (), 0.0)
    if schedule.minimum_duration > MAX_DURATION_SECONDS:
        fail((), BridgeError("limit_exceeded", "Sequence duration exceeds 600 seconds", 422))
    if schedule.native_width > MAX_PARALLEL_COMMANDS:
        fail(
            (),
            BridgeError(
                "limit_exceeded", "Sequence may exceed four concurrent native actions", 422
            ),
        )
    behavior_steps = [
        step for step in steps if step.node["type"] == "behavior" and step.command is not None
    ]
    if behavior_steps:
        names = await installed_names(
            core, {step.node["arguments"]["name"] for step in behavior_steps}
        )
        for step in behavior_steps:
            if step.node["arguments"]["name"] not in names:
                fail(step.path, BridgeError("unsupported", "Installed behavior unavailable", 422))
    if core.generation != generation:
        raise BridgeError("robot_unavailable", "Connection changed during sequence validation", 503)
    return SequencePlan(normalized, steps, schedule, diagnostics, generation)


@dataclass
class Run:
    parent: Operation
    plan: SequencePlan
    children: dict[tuple[int, ...], Operation]
    abort: asyncio.Event
    managed: set[str]
    reason: str | None = None
    failure_path: tuple[int, ...] | None = None
    stop_uncertain: bool = False


class Sequences:
    def __init__(self, core: Core):
        self.core = core
        self.runs: dict[str, Run] = {}

    def submit(self, session: Session, plan: SequencePlan) -> dict[str, Any]:
        if plan.generation != self.core.generation:
            raise BridgeError("robot_unavailable", "Connection changed during validation", 503)
        if session.public["status"] != "active" or session.deadline <= self.core.clock():
            raise BridgeError("session_invalid", "Sequence session is no longer active", 409)
        if plan.diagnostics:
            raise BridgeError(
                "invalid_sequence",
                "Sequence validation failed",
                422,
                {
                    "diagnostics": plan.diagnostics,
                },
            )
        if any(
            self.core.stop_pending(step.command.kind if step.command else None)
            for step in plan.steps
        ):
            raise BridgeError("resource_busy", "Priority stop is in progress", 409)
        parent = self.core.new_operation(
            session,
            SEQUENCE_KIND,
            sorted(plan.schedule.resources),
            history_slots=1 + len(plan.steps),
            native_slots=plan.schedule.native_width,
        )
        children = {
            step.path: self.core.sequence_child(session, parent, step) for step in plan.steps
        }
        run = Run(parent, plan, children, asyncio.Event(), set())
        self.runs[parent.public["id"]] = run
        self.core.update(parent, child_operation_ids=[op.public["id"] for op in children.values()])
        parent.task = self.core.spawn(self.execute(run))
        return copy.deepcopy(parent.public)

    def stop(self, run: Run, reason: str, *, managed: set[str] | None = None) -> None:
        if managed is not None:
            run.managed.update(managed)
        if run.abort.is_set():
            return
        run.reason = reason
        run.abort.set()
        if not run.parent.done.is_set():
            self.core.update(run.parent, status="cancelling")
        for op in run.children.values():
            if op.public["status"] == "accepted":
                self.core.finish(op, "cancelled", {"dispatched": False, "reason": reason})

    def interrupt(
        self, reason: str, *, session_id: str | None = None, scope: str | None = None
    ) -> list[Run]:
        runs = []
        for run in self.runs.values():
            if (
                run.parent.released
                or session_id is not None
                and run.parent.public["session_id"] != session_id
            ):
                continue
            children = list(run.children.values())
            matched = [
                child
                for child in children
                if scope is None or self.core.motion_stop_matches(scope, child.public["kind"])
            ]
            if not matched:
                continue
            self.stop(run, reason, managed={op.public["id"] for op in matched})
            runs.append(run)
        return runs

    async def finish_interruption(self, runs: list[Run], *, force: bool = False) -> list[str]:
        if not runs:
            return []
        try:
            async with asyncio.timeout(self.core.settings.backend_timeout_seconds):
                await asyncio.gather(*(run.parent.done.wait() for run in runs))
        except TimeoutError:
            pass
        unresolved = []
        for run in runs:
            if not run.parent.released:
                unresolved.append(run.parent.public["id"])
                if (
                    force
                    and run.parent.done.is_set()
                    and all(child.released for child in run.children.values())
                ):
                    self.core.release_operation(run.parent)
                    for child in run.children.values():
                        child.retained_until = run.parent.retained_until
        return sorted(unresolved)

    async def execute(self, run: Run) -> None:
        if not run.abort.is_set():
            self.core.start_operation(run.parent)
        try:
            async with asyncio.TaskGroup() as group:
                timer = group.create_task(self.expire(run))
                cancellation = group.create_task(self.cancel_children(run))
                await self.execute_node(run, run.plan.document["root"], ())
                timer.cancel()
                if not run.abort.is_set():
                    cancellation.cancel()
        except Exception:
            LOG.exception("Sequence execution failed id=%s", run.parent.public["id"])
            self.stop(run, "sequence_interrupted")
            await self.cancel_children(run)
        self.finish(run)

    async def expire(self, run: Run) -> None:
        await asyncio.sleep(MAX_DURATION_SECONDS)
        self.stop(run, "sequence_deadline")

    async def cancel_children(self, run: Run) -> None:
        await run.abort.wait()
        targets = [
            op
            for op in run.children.values()
            if not op.done.is_set()
            and op.public["kind"] != DELAY_KIND
            and op.public["id"] not in run.managed
        ]
        async with asyncio.TaskGroup() as group:
            for child in targets:
                group.create_task(self.cancel_child(run, child))

    async def cancel_child(self, run: Run, child: Operation) -> None:
        await self.core.cancel(child, from_sequence=True)
        if child.cancel_task is not None and not await child.cancel_task:
            run.stop_uncertain = True

    async def execute_node(self, run: Run, node: dict[str, Any], path: tuple[int, ...]) -> None:
        if run.abort.is_set():
            return
        kind = node["type"]
        if kind == "sequence":
            for index, child in enumerate(node["steps"]):
                if run.abort.is_set():
                    break
                await self.execute_node(run, child, (*path, index))
            return
        if kind == "parallel":
            async with asyncio.TaskGroup() as group:
                for index, child in enumerate(node["steps"]):
                    group.create_task(self.execute_node(run, child, (*path, index)))
            return
        child = run.children[path]
        if kind == "delay":
            self.core.start_operation(child)
            try:
                await asyncio.wait_for(run.abort.wait(), node["duration"])
            except TimeoutError:
                self.core.finish(child, "succeeded", {"duration": node["duration"]})
            else:
                self.core.finish(child, "cancelled", {"dispatched": False})
            return
        try:
            command = prepare(self.core, kind, node["arguments"], ready=True)
            if set(command.resources) != set(child.public["resources"]):
                raise BridgeError("resource_busy", "Step resources changed after validation", 409)
            child.retained_resources = command.retained_resources
            child.task = self.core.spawn(self.core.run_native(child, command.payload))
            await child.done.wait()
        except BridgeError as exc:
            self.core.finish(child, "failed", {"dispatched": False}, exc)
        if child.public["status"] != "succeeded" and not run.abort.is_set():
            run.failure_path = path
            self.stop(run, "child_failed")

    def finish(self, run: Run) -> None:
        uncertain = run.stop_uncertain or any(not op.released for op in run.children.values())
        if uncertain:
            status = "unknown"
            error = BridgeError(
                "sequence_outcome_unknown", "A sequence child has an unresolved outcome", 503
            )
        elif run.reason in {"child_failed", "sequence_deadline", "sequence_interrupted"}:
            status = "failed"
            error = BridgeError(
                "sequence_" + run.reason.removeprefix("sequence_"), "Sequence did not complete", 422
            )
        elif run.abort.is_set():
            status, error = "cancelled", None
        else:
            status, error = "succeeded", None
        result = {
            "reason": run.reason,
            "failed_path": list(run.failure_path) if run.failure_path is not None else None,
            "children": [
                {"path": list(path), "operation_id": op.public["id"], "status": op.public["status"]}
                for path, op in run.children.items()
            ],
        }
        self.core.finish(run.parent, status, result, error)
        if run.parent.retained_until is not None:
            for child in run.children.values():
                child.retained_until = run.parent.retained_until

    def reconcile(self, parent_id: str) -> None:
        run = self.runs.get(parent_id)
        if (
            run is not None
            and not run.parent.released
            and run.parent.public["status"] == "unknown"
            and not run.stop_uncertain
            and all(child.released for child in run.children.values())
        ):
            self.finish(run)

    def prune(self) -> None:
        for oid in list(self.runs):
            if oid not in self.core.operations:
                del self.runs[oid]
