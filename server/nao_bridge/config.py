import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    robot_id: str = "nao"
    host: str = "127.0.0.1"
    port: int = 3000
    backend: str = "naoqi"
    robot_address: str = "127.0.0.1"
    robot_port: int = 9559
    agent_python: str = "/opt/legacy/bin/python2.7"
    agent_script: Path = Path("/opt/nao-bridge/agent/agent.py")
    sdk_path: str = "/opt/legacy/qi167"
    legacy_library_path: str = "/usr/lib/x86_64-linux-gnu:/opt/legacy/lib"
    lease_seconds: float = 30.0
    history_seconds: float = 900.0
    max_sessions: int = 64
    max_session_history: int = 4096
    max_operations: int = 4096
    max_recovery_operations: int = 128
    max_idempotency_keys: int = 4096
    max_subscriptions: int = 16
    max_listeners: int = 32
    max_listener_history: int = 256
    asset_directory: Path | None = None
    asset_quota_bytes: int = 268435456
    max_assets: int = 256
    max_asset_downloads: int = 2
    asset_retention_seconds: float = 604800
    max_recordings: int = 4
    max_recording_history: int = 256
    max_recording_seconds: float = 300
    max_recording_bytes: int = 33554432
    recording_queue_count: int = 64
    recording_queue_bytes: int = 1048576
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
    # Tests inject time at the core boundary; transport scheduling uses the real loop clock.
    clock: Callable[[], float] | None = field(default=None, repr=False, compare=False)

    def validate(self) -> None:
        if self.backend not in ("naoqi", "fake", "native-fixture"):
            raise ValueError("Unknown backend")
        if self.lease_seconds <= 0 or self.history_seconds <= 0:
            raise ValueError("Lease and history retention must be positive")
        if not 0 < self.sample_rate_hz <= 10:
            raise ValueError("Sample rate must be in (0, 10]; faster acquisition is unverified")
        if (
            not math.isfinite(self.asset_retention_seconds)
            or self.asset_retention_seconds <= 0
            or not math.isfinite(self.max_recording_seconds)
            or not 0 < self.max_recording_seconds <= 300
        ):
            raise ValueError("Asset retention and recording duration must be positive")
        if not 65536 <= self.max_recording_bytes <= 33554432:
            raise ValueError("Recording byte limit must be between 64 KiB and 32 MiB")
        for name in (
            "max_sessions",
            "max_session_history",
            "max_operations",
            "max_recovery_operations",
            "max_idempotency_keys",
            "max_subscriptions",
            "max_listeners",
            "max_listener_history",
            "asset_quota_bytes",
            "max_assets",
            "max_asset_downloads",
            "max_recordings",
            "max_recording_history",
            "recording_queue_count",
            "recording_queue_bytes",
            "max_streams",
            "max_streams_per_session",
            "event_queue_count",
            "event_queue_bytes",
            "max_request_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(name + " must be positive")
