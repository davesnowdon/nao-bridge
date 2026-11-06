# Project decisions

- The replacement API has no v1, FluentNao, or existing application compatibility requirement. FluentNao is inspiration, not a public contract. Prefer a small, coherent API for common robot tasks over exposing every NAOqi method.
- Events use SSE; commands use HTTP. The default deployment must not require MQTT or another container. MQTT is only a possible later integration.
- The language-neutral HTTP/SSE contract is authoritative; Python is its first client. Keep robot semantics/defaults on the server and document client lifecycle behavior independently of Python. Future Rust/other clients are design considerations, not implementation scope for the four increments.
- Ubuntu's packaged Flask is intentional: the old Python 2 dependencies could no longer be fetched from PyPI. Preserve that dependency source unless a replacement is demonstrated in the SDK container; the unused requirements file does not identify the installed Flask version.
- The target design and four delivery increments are in [docs/api-plan.md](docs/api-plan.md) and [docs/delivery-plan.md](docs/delivery-plan.md). They describe planned behavior, not implemented capabilities.
- Use modern Recorder and Wanderer applications, plus a scripted presentation, as acceptance cases. Keep their UI, editing, mapping, and planning logic outside the bridge.
- Research evidence and unresolved hardware questions are in [docs/research-notes.md](docs/research-notes.md). Distinguish reproduced software defects from unverified hardware behavior.
