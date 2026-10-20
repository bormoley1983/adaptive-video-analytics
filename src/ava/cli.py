"""AVA command-line entry point.

Exposes a single `ava run` command that takes a config file and an optional
GPU override. This is the reproducible command recorded in the baseline report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .runner import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ava", description="Adaptive Video Analytics")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one detection pass over a clip")
    run_p.add_argument("--config", required=True, type=Path, help="YAML run config")
    run_p.add_argument("--gpu", default=None, help="Override GPU (index or UUID)")

    args = parser.parse_args(argv)

    if args.command == "run":
        config = load_config(args.config)
        if args.gpu is not None:
            config.gpu = args.gpu
        summary = run(config)
        print(json.dumps(summary.to_dict(), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
