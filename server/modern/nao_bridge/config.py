from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    robot_id: str = "nao"
    host: str = "127.0.0.1"
    port: int = 3000
    token: str | None = None
    operator_token: str | None = None
    origins: tuple[str, ...] = ()
    backend: str = "naoqi"
    robot_address: str = "127.0.0.1"
    robot_port: int = 9559
    agent_python: str = "/opt/legacy/bin/python2.7"
    agent_script: Path = Path("/opt/nao-bridge/agent/agent.py")
    sdk_path: str = "/nao-bridge/lib/pynaoqi-python2.7-2.1.4.13-linux64"
    legacy_library_path: str = "/opt/legacy/sdk-sonames:/opt/legacy/lib"
    lease_seconds: float = 30.0
    history_seconds: float = 900.0
    max_sessions: int = 64
    max_operations: int = 4096
    max_recovery_operations: int = 128
    max_idempotency_keys: int = 4096
    max_subscriptions: int = 16
    max_streams: int = 8
    max_streams_per_session: int = 4
    event_queue_count: int = 256
    event_queue_bytes: int = 262144
    max_request_bytes: int = 16384
    max_state_wait_ms: int = 2000
    sample_rate_hz: float = 10.0
    sse_heartbeat_seconds: float = 10.0
    sse_write_timeout_seconds: float = 2.0
    backend_timeout_seconds: float = 2.0
    shutdown_timeout_seconds: float = 3.0
    data_dir: Path | None = None
    # Tests inject time at the core boundary; transport scheduling uses the real loop clock.
    clock: Callable[[], float] | None = field(default=None, repr=False, compare=False)

    def validate(self) -> None:
        if self.host not in ("127.0.0.1", "::1", "localhost") and not self.token:
            raise ValueError("A bearer token is required for non-loopback bindings")
        if self.operator_token and self.operator_token == self.token:
            raise ValueError("Operator and client tokens must differ")
        if self.backend not in ("naoqi", "fake", "native-fixture"):
            raise ValueError("Unknown backend")
        if self.lease_seconds <= 0 or self.history_seconds <= 0:
            raise ValueError("Lease and history retention must be positive")
        if self.sample_rate_hz <= 0 or self.sample_rate_hz > 20:
            raise ValueError("Sample rate must be in (0, 20]")
        for name in (
            "max_sessions",
            "max_operations",
            "max_recovery_operations",
            "max_idempotency_keys",
            "max_subscriptions",
            "max_streams",
            "max_streams_per_session",
            "event_queue_count",
            "event_queue_bytes",
            "max_request_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(name + " must be positive")
