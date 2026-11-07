"""Test-only bootstrap for the installed API on an inherited HTTP socket."""

from __future__ import annotations

import hashlib
import json
import signal
import socket
import sys
from contextlib import asynccontextmanager
from importlib import import_module
from importlib.metadata import distribution
from pathlib import Path


def installed_api_provenance() -> dict:
    package = distribution("ava")
    origin = json.loads(package.read_text("direct_url.json") or "{}")
    if origin.get("dir_info", {}).get("editable"):
        raise RuntimeError("API acceptance requires a non-editable installation")
    prefix = Path(sys.prefix).resolve()
    modules = {}
    module_hashes = {}
    for name in ("ava", "ava.api.app", "ava.infrastructure.jwks_verifier",
                 "ava.persistence.postgres"):
        path = Path(import_module(name).__file__).resolve()
        if not path.is_relative_to(prefix) or "site-packages" not in path.parts:
            raise RuntimeError("API acceptance cannot load source-checkout modules")
        modules[name] = str(path)
        module_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"noneditable": True, "version": package.version,
            "python_prefix": str(prefix), "modules": modules, "module_sha256": module_hashes}


def main():
    import uvicorn

    from ava.api.app import compose_postgres_app
    from ava.infrastructure.jwks_verifier import JwksRsaVerifier

    config = json.load(sys.stdin)
    proof = installed_api_provenance()
    proof.update(startup_complete=False, shutdown_complete=False)
    proof_path = Path(config["provenance_path"])
    proof_path.write_text(json.dumps(proof, indent=2))
    policy = JwksRsaVerifier(config["issuer"], config["jwks_uri"], "ava-api")
    app = compose_postgres_app(config["dsn"], identity_policy=policy)
    production_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def recorded_lifespan(application):
        async with production_lifespan(application):
            proof["startup_complete"] = True
            proof_path.write_text(json.dumps(proof, indent=2))
            yield
        proof["shutdown_complete"] = True
        proof_path.write_text(json.dumps(proof, indent=2))

    app.router.lifespan_context = recorded_lifespan
    server = uvicorn.Server(uvicorn.Config(
        app, access_log=False, log_level="warning", lifespan="on",
        timeout_graceful_shutdown=5,
    ))
    # Uvicorn re-delivers shutdown signals to the previous handler after cleanup.
    signal.signal(signal.SIGTERM, lambda *_: setattr(server, "should_exit", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(server, "should_exit", True))
    with socket.socket(fileno=int(sys.argv[1])) as listener:
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
