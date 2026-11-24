# NAO Bridge presentation example

Start the fake bridge from the repository root in a separate terminal.

```sh
make -C server run
```

With the bridge running, inspect the presentation profile and validate the
choreography. Validation does not change the robot.

```sh
uv run --project clients/python python examples/presentation/present.py
```

Add `--run` to apply the profile, wake, stand, speak while waving and changing
eye lights, then sit and rest. The example prints each child's actual outcome.

```sh
uv run --project clients/python python examples/presentation/present.py --run
```

Use `--bridge` to choose another endpoint. The bridge advertises whether it can
run each action. The wave and animated speech currently require a fixture.
On a robot where the actions are enabled, use a clear, stable surface and
allow space for the robot to stand, wave and sit.

`--animated` adds a separate animated utterance after standing. Animated speech
owns the joints and lights, so the wave and eye transition stay in the parallel
step with plain speech.

`--allow-partial-profile` accepts a presentation profile with unavailable
settings and prints the effects and gaps. `--restore-on-close` asks the bridge to
restore eligible autonomy changes after session cleanup. Restoring autonomous
life can start native activity. An interruption or wait timeout closes the
session and requests cleanup of any running sequence.

The [portable choreography](../../contracts/fixtures/presentation-sequence.json)
contains the plain sequence as a hand-authored HTTP request body.
