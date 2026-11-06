# Project decisions

- The replacement API has no v1, FluentNao, or existing application compatibility requirement. FluentNao is inspiration, not a public contract. Prefer a small, coherent API for common robot tasks over exposing every NAOqi method.
- Events use SSE without historical replay in the first contract; reconnect reports a gap and fresh state. Commands use HTTP. No required MQTT or extra container.
- The language-neutral HTTP/SSE contract is authoritative; Python is its first client. Keep robot semantics/defaults on the server and document client lifecycle behavior independently of Python. Future Rust/other clients are design considerations, not implementation scope for the four increments.
- Use a thin Python 2 NAOqi agent and a modern API process in one container. Modern Python/ASGI is preferred; prove packaging in increment 1A. Rust is a fallback, not a prerequisite for future Rust clients. See [docs/runtime-design.md](docs/runtime-design.md).
- Ubuntu's packaged Flask was intentional because legacy dependencies were unavailable from PyPI. Preserve the working SDK environment during the split; this does not require retaining Flask once the modern process owns HTTP.
- Retain recordings/assets and server-side sequences, including parallel execution and child cancellation. Explicit advance session reservations and event replay are deferred; command ownership and sequence reservations remain required.
- The target design and four delivery increments are in [docs/api-plan.md](docs/api-plan.md) and [docs/delivery-plan.md](docs/delivery-plan.md). They describe planned behavior, not implemented capabilities.
- Use modern Recorder and Wanderer applications, plus a scripted presentation, as acceptance cases. Keep their UI, editing, mapping, and planning logic outside the bridge.
- Research evidence and unresolved hardware questions are in [docs/research-notes.md](docs/research-notes.md). Distinguish reproduced software defects from unverified hardware behavior.
