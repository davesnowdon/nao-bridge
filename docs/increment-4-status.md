# Increment 4 implementation

This branch continues the [increment 4 delivery plan](delivery-plan.md#increment-4--expression-composition-media-and-complete-release)
on the increment 3 Wanderer branch. Physical acceptance remains open on
NAO6/NAOqi 2.8 and NAOv5/NAOqi 2.1.

## Lights

Contract 1.10.0 delivers common light groups, bounded color/brightness fades,
shared light samples, and sync/async Python SDK helpers. Discovery and ownership
use actual native group membership. A combined eye group holds both eye
resources, and disjoint groups can run concurrently. Successful fades retain
ownership for the session. Session cleanup leaves the resulting light levels
in place.

The agent reads actuator levels in one ALMemory batch on the shared sampler.
Missing or mixed levels are explicit. Native fade completion, actuator readings
and visible output are separate observations. An accepted cancellation can
fence queued work. An in-flight fade retains ownership until confirmed completion
or explicit recovery.

The native-container suite exercised RGB and monochrome writes and read-back,
mixed group levels, and light samples over SSE using SDK-independent HTTP.
`make check` passed 612 server/integration tests and 47 SDK tests, with
coverage of resource overlap, cancellation, session cleanup, and both Python
clients. The contract compatibility guard passed against PR #38.

Physical acceptance still needs to confirm group discovery, color and brightness
on both robots, the effect of robot-side autonomous changes, and the response
when a fade is interrupted.

See [the lights HTTP/SDK contract](http-api.md#lights),
[NAOqi group discovery and fades](https://doc.aldebaran.com/2-1/naoqi/sensors/alleds-api.html),
and the documented LED layouts for
[NAOv5](https://doc.aldebaran.com/2-1/family/robots/leds_robot.html) and
[NAO6](https://doc.aldebaran.com/2-8/family/nao_technical/leds_naov6.html).
