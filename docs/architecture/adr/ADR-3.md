# ADR-3: explicit detector and prompt-profile lifecycles

Date: 2026-10-19 · Status: Accepted for PoC; optimized export path provisional · Scope: E-1/E-3/E-5.

## Context and decision

Object labels, temporal rules and semantic interpretation are distinct capabilities. Start with the current released stable Ultralytics families: **YOLO26n/s** for conventional detection and **YOLOE-26 small**, initially `yoloe-26s-seg.pt`, for text-configurable object classes, subject to E-1 compatibility/quality checks. Recheck official releases when implementation begins and prefer a newer stable family if validated. YOLO11 is an optional historical comparison or justified fallback, not the project default. Record exact package/checkpoint revisions, hashes, class ordering, preprocessing, confidence/NMS settings and text encoder assets in every run manifest. See official [YOLO26](https://docs.ultralytics.com/models/yolo26/) and [YOLOE](https://docs.ultralytics.com/models/yoloe/) documentation (checked 2026-10-19).

Wrap models behind a common metadata contract. In Python, configure YOLOE classes before a run; do not imply interactive prompt changes are already supported. The optimized path uses immutable, versioned prompt profiles: export/build an engine per supported profile, validate it, then activate it deliberately with a new model/config version. Retain the previous valid profile for rollback. Never mutate class order without changing the profile identity.

Ultralytics documents that YOLOE static exports bake in classes and reject runtime class/visual-prompt changes. Runtime text reconfiguration therefore requires a separately validated backend or a rebuild/swap workflow; this is a product tradeoff to resolve in B-005. Export feasibility also requires testing output parsing and postprocessing, not merely producing an engine file. See [YOLOE documentation](https://docs.ultralytics.com/models/yoloe/) and [TensorRT export guidance](https://docs.ultralytics.com/integrations/tensorrt/) (checked 2026-10-19).

## Alternatives and consequences

One universal inference backend would hide important capability differences. Adopting Grounding DINO/VLM for every frame would add cost before value is measured. Explicit profiles improve reproducibility but add engine build/storage/reload latency and may not satisfy instant prompt updates. No detector output alone implements unattended-object duration or semantic behavior rules.

## Validation and revisit

E-1 verifies checkpoints, text encoder downloads and prompt behavior. E-3 tests engine equivalence and profile activation/reload disruption, reporting speed separately from reconfiguration cost. Revisit on runtime-label requirements, export failures, license constraints or meaningful measured quality improvements. All model licenses/checkpoint terms must be recorded before adoption; see ADR-7.
