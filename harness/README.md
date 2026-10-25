# AVA RTSP Replay Harness (E-3-T2 / B-019)

Standalone CPU-only Compose project providing deterministic RTSP sources for
multi-stream capacity testing. One MediaMTX server plus one independent FFmpeg
publisher per emulated camera. Publishers loop existing recordings mounted
read-only, re-encode to a uniform H.264 720p @ 10 FPS stream, and publish to a
stable RTSP path (ADR-2).

## Bring up the full study stack (one command)

`harness/stack.sh` is the single idempotent entry point for the whole local
study stack — system prereqs, footage, MediaMTX, N publishers, and the
observability platform (Prometheus/Loki/Alloy/Grafana). It is safe to re-run:
it converges to a known-good state instead of double-starting anything.

```bash
make up                 # full stack, all cameras defined in compose
make up N=16            # only the first 16 cameras
./harness/stack.sh up --no-ops   # harness only, skip observability
make status             # one-screen health table (cams, mediamtx, ops, GPU)
make probe              # verify streams: codec / resolution / FPS / rate
make down               # ordered teardown (volumes preserved)
```

### Why a single script (the reboot story)

The publishers depend on MediaMTX by hostname. If the RTSP server is not up
when they start (e.g. right after a host reboot), each publisher crash-loops on
DNS and **exhausts its `on-failure` restart budget**, leaving it stuck in
`Exited` — it will no longer auto-recover, and a plain `docker compose up -d`
will not restart it. This is exactly what happened after a scheduled shutdown:
46 of 50 publishers were left dead.

`stack.sh up` avoids and repairs this by:

1. **Starting MediaMTX first** and polling its healthcheck until it is `healthy`.
2. **Then** starting the publishers, so the hostname always resolves.
3. **Self-healing**: any publisher not `Up` (including restart-exhausted
   `Exited` ones) is `--force-recreate`d, then the script polls until all N are
   `Up`.

So after any reboot or partial outage, one `make up` converges the whole stack.

### Camera count

The camera list is derived from `manifest.yaml` / `docker-compose.yml`, so the
script and `probe.sh` always match the harness size (4 on `develop`, 50 in the
E-3-T5 worktree). To scale to a new count (e.g. 100), add the matching entries
to both files, then `make up N=100`.

### Observability prerequisites

`stack.sh up` starts the ops stack too and creates `ops/.env` from
`ops/.env.example` if missing. Set a real `GRAFANA_ADMIN_PASSWORD` in
`ops/.env` before relying on Grafana. Use `--no-ops` (or run the harness
compose directly) for pure capacity runs that don't need dashboards.

## Files

## Topology

```
ava-cam-01 (FFmpeg) ──┐
ava-cam-02 (FFmpeg) ──┤──► ava-mediamtx :8554 (127.0.0.1 only) ──► probe / analytics
ava-cam-03 (FFmpeg) ──┤
ava-cam-04 (FFmpeg) ──┘
```

All containers carry the `ava-` prefix so the whole AVA stack (analytics +
harness) is manageable as one group.

- **No GPU** is allocated to the replay stack.
- **Local bind only**: RTSP is exposed on `127.0.0.1:8554`.
- **Read-only footage**: each publisher mounts only its own clip from `../videos/`.
- **Stable identity**: each camera has a fixed ID, RTSP path, and source mapping
  defined in `manifest.yaml`.

## Files

| File | Purpose |
| --- | --- |
| `stack.sh` | Single idempotent, self-healing bring-up for the full study stack (`up`/`down`/`status`) |
| `docker-compose.yml` | MediaMTX + N FFmpeg publishers, pinned by digest |
| `mediamtx.yml` | MediaMTX config: one path per camera, local bind, no auth |
| `manifest.yaml` | Deterministic camera→source map + target rates (drives camera count) |
| `probe.sh` | Acceptance script: probe all streams, verify codec/res/FPS/rate |

## Usage

### Start the harness

```bash
docker compose -f harness/docker-compose.yml up -d
```

Wait ~10 s for publishers to connect and MediaMTX to register paths.

### Probe all streams

```bash
./harness/probe.sh
```

Verifies each of the 4 RTSP paths is independently probeable, H.264 720p,
~10 FPS sustained.

### Restart a single publisher (reconnect test)

```bash
docker compose -f harness/docker-compose.yml restart ava-cam-02
# wait ~5 s, then re-probe:
./harness/probe.sh
```

The `restart: on-failure` policy and `-reconnect 1` flag ensure the publisher
re-establishes its RTSP session automatically.

### Restart MediaMTX (server recovery test)

```bash
docker compose -f harness/docker-compose.yml restart ava-mediamtx
# wait ~10 s for publishers to reconnect, then re-probe:
./harness/probe.sh
```

### Stop the harness

```bash
docker compose -f harness/docker-compose.yml down
```

## Measuring generator load separately

The replay stack's CPU/RAM usage is independent of the analytics worker. To
measure generator load without conflating it with downstream capacity:

```bash
# Per-container CPU/memory while the harness runs:
docker stats --no-stream $(docker compose -f harness/docker-compose.yml ps -q)
```

Record this separately from any analytics-worker measurements (ADR-2).

## Scaling to 16 / 25 / 50 streams

Add entries to `manifest.yaml` and corresponding publisher services in
`docker-compose.yml` following the same pattern. The MediaMTX config gains one
path per camera. Keep the manifest deterministic: camera ID, source file, and
RTSP path are all derived from the manifest entry.

## Acceptance criteria (B-019)

- [x] All four streams independently probeable
- [x] Sustain declared input rate (~10 FPS)
- [x] Preserve identity (stable camera ID / RTSP path)
- [x] Recover after individual publisher restart
- [x] Recover after MediaMTX restart
- [x] No generator bottleneck before downstream capacity is measured

## Generator CPU note (the `-re` fix)

Publishers must run with `-re` (real-time pacing) **before** the input. Without
it, FFmpeg reads the looping clip as fast as possible and saturates CPU (~120%
per publisher, ~7 cores total for 4 streams). With `-re`, each publisher paces
at the stream's native 10 FPS and steady-state cost drops to ~0.3% per
publisher and ~1.4% for MediaMTX — effectively free, so the harness cannot be
the generator bottleneck when downstream capacity is measured.

```
ffmpeg -re -stream_loop -1 -i /footage/normalized/cam-NN.mp4 \
       -map 0:v:0 -c:v copy -an -rtsp_transport tcp -f rtsp rtsp://ava-mediamtx:8554/cam-NN
```

Transport is TCP (set in both `docker-compose.yml` and `mediamtx.yml`). With
`-re` in place, transport no longer affects CPU; TCP is kept for loss-free
loopback delivery.
