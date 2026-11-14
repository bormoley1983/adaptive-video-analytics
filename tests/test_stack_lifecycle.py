"""Exercise launcher failures without accessing a Docker daemon."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("action,failure,expected", [
    ("up", "config", 1), ("down", "ops", 1), ("down", "harness", 1),
    ("down", "none", 0),
])
def test_launcher_reports_failure_before_claiming_success(tmp_path, action, failure, expected):
    source = Path(__file__).resolve().parents[1]
    for name in ("harness", "ops", "bin"):
        (tmp_path / name).mkdir()
    script = tmp_path / "harness/stack.sh"
    shutil.copyfile(source / "harness/stack.sh", script)
    for name in ("harness/docker-compose.yml", "ops/docker-compose.yml", "ops/.env"):
        (tmp_path / name).write_text("# isolated launcher test\n")
    docker = tmp_path / "bin/docker"
    docker.write_text("""#!/bin/bash
printf '%s\n' "$*" >> "$DOCKER_CALLS"
case "$*" in
  *'config --services'*) echo ava-cam-01 ;;
  *'config --quiet'*) [[ "$FAILURE" != config ]] ;;
  *'/ops/docker-compose.yml down'*) [[ "$FAILURE" != ops ]] ;;
  *'/harness/docker-compose.yml down'*) [[ "$FAILURE" != harness ]] ;;
  *) exit 0 ;;
esac
""")
    docker.chmod(0o755)
    calls = tmp_path / "calls.log"
    result = subprocess.run(
        ["bash", str(script), action, *(["1"] if action == "up" else [])],
        env={**os.environ, "PATH": f"{docker.parent}:{os.environ['PATH']}",
             "DOCKER_CALLS": str(calls), "FAILURE": failure},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    commands = calls.read_text()
    if action == "up":
        assert "config --quiet" in commands
        assert " up " not in commands
        assert "stack is up" not in result.stdout
    else:
        assert "/ops/docker-compose.yml down" in commands
        assert "/harness/docker-compose.yml down" in commands
        assert ("stack is down" in result.stdout) == (expected == 0)
