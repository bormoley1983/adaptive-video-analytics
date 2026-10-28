"""AVA command-line entry point.

Exposes `ava run` (file-based) and `ava live` (RTSP multi-camera) commands,
each taking a config file and an optional GPU override.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config, load_live_config
from .runner import run


def _verify_persistence(config) -> None:
    """Verify the persistence backend is ready before starting a run.

    For PostgreSQL: constructs the sink (which connects and verifies the
    schema is migrated to the expected Alembic revision), then closes it.
    For JSONL: no preflight needed (file I/O is verified by the runner).
    Raises a clear error if the backend is unavailable or unmigrated.
    """
    if config.persistence.backend != "postgres":
        return

    from .persistence.compose import build_event_sink

    sink = build_event_sink(config.persistence)
    sink.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ava", description="Adaptive Video Analytics")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one detection pass over a clip")
    run_p.add_argument("--config", required=True, type=Path, help="YAML run config")
    run_p.add_argument("--gpu", default=None, help="Override GPU (index or UUID)")

    live_p = sub.add_parser("live", help="Run live multi-camera RTSP analytics")
    live_p.add_argument("--config", required=True, type=Path, help="YAML live run config")
    live_p.add_argument("--gpu", default=None, help="Override GPU (index or UUID)")

    args = parser.parse_args(argv)

    if args.command == "run":
        config = load_config(args.config)
        if args.gpu is not None:
            config.gpu = args.gpu
        if config.cameras:
            _verify_persistence(config)
        summary = run(config)
        print(json.dumps(summary.to_dict(), indent=2))
        return 0

    if args.command == "live":
        from .config import DetectorConfig
        from .live_runner import run_live

        with args.config.open("r", encoding="utf-8") as handle:
            import yaml
            raw = yaml.safe_load(handle) or {}
        live_config = load_live_config(args.config)
        detector_raw = raw.get("detector", {})
        detector_config = DetectorConfig.model_validate(detector_raw)
        gpu = args.gpu if args.gpu is not None else raw.get("gpu", "0")
        output_dir = Path(raw.get("output_dir", "results"))

        _verify_persistence(live_config)
        summary = run_live(live_config, detector_config, output_dir, gpu=gpu)
        print(json.dumps(summary.to_dict(), indent=2))
        return 0

    # Unreachable: argparse requires a subcommand (required=True).
    return 1  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    sys.exit(main())
