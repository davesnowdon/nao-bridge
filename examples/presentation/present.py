"""Run a short server-side choreography through the public Python client."""

import argparse
import sys
from typing import Any

from nao_bridge_client import (
    BridgeError,
    Nao,
    OperationCancelled,
    OperationData,
    OperationFailed,
    OperationUnknown,
    actions,
    parallel,
    sequence,
)


def choreography(text: str, *, animated: bool) -> dict[str, Any]:
    prelude = [actions.say("Welcome to the presentation.", animated=True)] if animated else []
    return sequence(
        actions.wake(),
        actions.posture("StandInit"),
        *prelude,
        parallel(
            actions.say(text),
            actions.gesture("wave", side="right"),
            actions.lights("eyes", color="#0080ff", duration=0.5),
        ),
        actions.posture("Sit"),
        actions.rest(),
    )


def step_label(path: list[int]) -> str:
    return ".".join(str(index + 1) for index in path) or "sequence"


def report_settings(settings: list[dict[str, Any]]) -> None:
    for item in settings:
        detail = item.get("reason") or item.get("rollback")
        print(f"  {item['setting']} {item['status']} ({detail or item['observed']})")


def report(operation: OperationData) -> None:
    print(f"{operation.kind} {operation.id} {operation.status}")
    result = operation.result or {}
    report_settings(result.get("settings", []))
    for child in result.get("children", []):
        print(f"  Step {step_label(child['path'])} {child['status']} ({child['operation_id']})")


def present(nao: Nao, args: argparse.Namespace) -> int:
    profile = next(
        (item for item in nao.autonomy.profiles().items if item.name == "presentation"), None
    )
    if profile is None:
        print("The bridge does not offer the presentation profile.", file=sys.stderr)
        return 1
    if profile.unavailable_settings:
        print("Unavailable profile settings " + ", ".join(profile.unavailable_settings))
        if not args.allow_partial_profile:
            return 1

    script = choreography(args.text, animated=args.animated)
    validation = nao.sequences.validate(script)
    if not validation.valid:
        for reason in validation.diagnostics:
            print(f"Step {step_label(reason.path)} {reason.message}", file=sys.stderr)
        return 1
    estimate = validation.estimated_duration_seconds
    duration = f"approximately {estimate:g}s" if estimate is not None else "unknown duration"
    print(f"Validated {len(validation.timeline)} actions ({duration}).")
    if not args.run:
        return 0

    applied = nao.autonomy.apply(
        profile.name,
        allow_partial=args.allow_partial_profile,
        restore_on_close=args.restore_on_close,
    )
    report(applied.data)
    operation = nao.sequences.run(script, wait=False)
    print(f"Accepted presentation {operation.id}", flush=True)
    report(operation.wait(timeout=45).data)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", default="http://127.0.0.1:3000")
    parser.add_argument("--text", default="Hello from NAO Bridge.")
    parser.add_argument("--run", action="store_true", help="Apply the profile and move the robot")
    parser.add_argument(
        "--animated", action="store_true", help="Add a separate animated speech prelude"
    )
    parser.add_argument("--allow-partial-profile", action="store_true")
    parser.add_argument(
        "--restore-on-close", action="store_true", help="Restore eligible settings on cleanup"
    )
    args = parser.parse_args(argv)
    nao = Nao(args.bridge)
    try:
        with nao:
            return present(nao, args)
    except (OperationFailed, OperationCancelled, OperationUnknown) as error:
        report(error.operation)
        print(error, file=sys.stderr)
        return 1
    except (BridgeError, TimeoutError) as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if nao.cleanup_result and nao.cleanup_result.result:
            restoration = nao.cleanup_result.result.get("autonomy_restoration")
            if restoration:
                print(f"Autonomy restoration {restoration['status']}")
                report_settings(restoration["settings"])


if __name__ == "__main__":
    raise SystemExit(main())
