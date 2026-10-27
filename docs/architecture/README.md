# Architecture decision records

ADRs describe a decision, its alternatives, consequences, validation, and revisit trigger. They are not proof that software has been implemented. Initial decisions below were prepared on 2026-10-19 from [ava-ideas.md](../ava-ideas.md).

| ADR | Decision | Status | Primary epics |
|---|---|---|---|
| [ADR-1](adr/ADR-1.md) | Python modular PoC and small initial stack | Accepted; E-1 validated | E-1/E-2 |
| [ADR-2](adr/ADR-2.md) | DeepStream candidate and independent GPU workers | Proposed pending benchmarks | E-3/E-4 |
| [ADR-3](adr/ADR-3.md) | Detector adapters and explicit prompt profiles | Accepted for PoC; export path provisional | E-1/E-3/E-5 |
| [ADR-4](adr/ADR-4.md) | Configuration, time and rule semantics | Accepted; partially implemented | E-2/E-3 |
| [ADR-5](adr/ADR-5.md) | Versioned events, evidence and staged integration | Event contract implemented; evidence integration incomplete | E-2/E-4/E-6 |
| [ADR-6](adr/ADR-6.md) | Reproducible quality and capacity gates | E-1 baseline validated; capacity pending | All |
| [ADR-7](adr/ADR-7.md) | Reproducible deployment and operational boundaries | Accepted for PoC; pilot controls proposed | All |
| [ADR-8](adr/ADR-8.md) | Optional bounded advanced-model verification | Proposed pending E-5/E-7 | E-5/E-6/E-7 |
| [ADR-9](adr/ADR-9.md) | Reuse external infrastructure, activate services incrementally | External integration retained; local development superseded by ADR-15 | E-3/E-6 |
| [ADR-10](adr/ADR-10.md) | Private documentation and manual remote handoff | Accepted | All |
| [ADR-11](adr/ADR-11.md) | Layered test harness and behavior coverage | Accepted; threshold superseded by ADR-13 | All |
| [ADR-12](adr/ADR-12.md) | Isolated agent worktrees, durable handoffs and mandatory human plan/commit gates | Accepted | All development |
| [ADR-13](adr/ADR-13.md) | Risk-based 90% coverage gate | Accepted | All development |
| [ADR-14](adr/ADR-14.md) | AVA-owned independent control and observation API | Accepted for design; implementation pending | F-1–F-6, X-1/X-2 |
| [ADR-15](adr/ADR-15.md) | Local containerized operations and PostgreSQL persistence | Accepted for staged implementation | E-3, F-1, F-3, F-5, E-6 |
| [ADR-16](adr/ADR-16.md) | Modular monolith with layered hexagonal architecture | Accepted for staged implementation | E-3, F-1–F-6, X-1/X-2 |
| [ADR-17](adr/ADR-17.md) | Bounded low-latency live frame flow and absolute analysis cadence | Accepted; reference flow validated | E-3/E-4 |
| [ADR-18](adr/ADR-18.md) | Deadline-bounded single-model micro-batching with per-camera tracking | Accepted for staged implementation; capacity pending | E-3/E-4 |

## Stack at a glance

| Concern | Initial project: E-1/E-2 | Target implementation: E-3–E-7 |
|---|---|---|
| Execution | GPU container validated on `ai-host1`; one RTX 3090 used and the local-AI workload preserved | Containers on measured NVIDIA GPUs; multi-GPU only in approved windows |
| Runtime | Python 3.12.3, torch 2.14.1+cu130, Ultralytics 8.4.183 measured; dependency lock still open in B-016 | Current stable DeepStream/GStreamer candidate; supported Python APIs; C++ for required plugins |
| Decode | OpenCV/FFmpeg MP4 | NVDEC via DeepStream; MediaMTX/FFmpeg test RTSP |
| Detection | Current stable PyTorch/Ultralytics; YOLO26n/s and YOLOE-26 small candidates | Current stable TensorRT; validated engines and explicit prompt-profile exports |
| Tracking/rules | ByteTrack + Python rule state | NvDCF candidate, `nvdsanalytics` + custom temporal logic |
| Contracts/config | Pydantic, JSON/YAML, JSONL outputs | Same versioned domain contracts and profile semantics |
| Events/storage | Versioned JSONL events implemented; local evidence store exists | PostgreSQL 18/outbox is canonical for services; JSONL remains an export; SeaweedFS is the selected S3 evidence adapter |
| Architecture | Small Python modules coordinated by CLI runners | Modular monolith: presentation, application and domain layers with infrastructure behind hexagonal ports; no mandatory facade |
| Event delivery | None | PostgreSQL outbox first; Kafka is the preferred future stream adapter after explicit activation gates |
| Async enrichment | None | Optional AVA-controlled model or bounded LiteLLM jobs when E-5/E-7 justify them |
| UI | Annotated MP4 | Minimal FastAPI server-rendered incident view |
| Observability | Benchmark JSON and E-3 runtime instrumentation in progress | Local Prometheus/Grafana and Loki/Alloy; bounded structured logs; GPU/decoder/host exporters after compatibility validation |
| Packaging | Dockerfile + Compose + CLI with a validated dependency lock; private docs manually transferred | Separate pinned GPU runtime, RTSP harness and CPU-only operations Compose projects; external adapters later; no Kubernetes initially |

This is the recommended fit for the stated hardware and delivery order, not an unconditional final stack. Apply [ADR-1](adr/ADR-1.md)'s latest mutually compatible stable policy to every component; an older version requires a documented reason. Accept or revise proposed ADRs at the owning epic gate. The original ideas document is historical input, not the authoritative version recommendation.

## C4 diagrams

The first three views describe the implemented offline PoC. Views four through
six are proposals constrained by ADR-2/5/8/9/14/15/16/18 and must not be read
as deployed state. They extend the existing E-series runtime/integration plan
with the F-series API and X-series UI work; they do not cancel those milestones.

- [System context](diagrams/01-system-context.puml)
- [Current containers](diagrams/02-current-containers.puml)
- [Current CLI components](diagrams/03-current-components.puml)
- [Proposed product and local-operations containers](diagrams/04-target-containers.puml)
- [Future modular-monolith layers and hexagonal adapters](diagrams/05-future-api-components.puml)
- [Proposed live batched-inference components](diagrams/06-live-batch-components.puml)

The files use the C4-PlantUML standard-library includes (`<C4/...>`). Rendering
requires a PlantUML distribution that contains that standard library.

## Change procedure

Create the next `ADR-N.md` for a new architectural choice. Include date/status, context, decision, alternatives, consequences, validation and revisit trigger. For a superseding decision, retain the old record, mark it superseded and link both ways. Update this index, affected epic and daily log together. Keep code-level specifications in [design](../design/README.md).
