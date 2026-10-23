# AVA RTSP Replay Harness (E-3-T2 / B-019)

Standalone CPU-only Compose project providing deterministic RTSP sources for
multi-stream capacity testing. One MediaMTX server plus one independent FFmpeg
publisher per emulated camera. Publishers loop existing recordings mounted
read-only, re-encode to a uniform H.264 720p @ 10 FPS stream, and publish to a
stable RTSP path (ADR-2).

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
| `docker-compose.yml` | MediaMTX + 4 FFmpeg publishers, pinned by digest |
| `mediamtx.yml` | MediaMTX config: 4 paths, local bind, no auth |
| `manifest.yaml` | Deterministic camera→source map + target rates |
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
