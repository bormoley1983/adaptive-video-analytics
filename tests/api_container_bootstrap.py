"""Run the installed identity-only API inside an owned test container."""

from __future__ import annotations

import json
import signal
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from api_http_bootstrap import installed_api_provenance


def main():
    import httpx
    import uvicorn

    from ava.api.app import AppConfig, create_app
    from ava.infrastructure.jwks_verifier import JwksRsaVerifier

    config = json.loads(sys.stdin.readline())
    issuer = config["issuer"]
    provider = urlsplit(issuer)
    addresses = sorted({record[4][0] for record in socket.getaddrinfo(
        provider.hostname, provider.port, type=socket.SOCK_STREAM,
    )})
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.get(issuer + "/.well-known/openid-configuration")
        response.raise_for_status()
        discovery = response.json()
    if discovery["issuer"] != issuer or discovery["jwks_uri"] != config["jwks_uri"]:
        raise RuntimeError("Provider discovery disagrees with configured issuer/JWKS")
    proof = installed_api_provenance()
    proof.update(issuer=issuer, jwks_uri=config["jwks_uri"], provider_addresses=addresses,
                 discovery_issuer=discovery["issuer"],
                 startup_complete=False, shutdown_complete=False)
    proof_path = Path("/tmp/api-provenance.json")
    proof_path.write_text(json.dumps(proof, indent=2))
    policy = JwksRsaVerifier(issuer, config["jwks_uri"], "ava-api")
    app = create_app(AppConfig(identity_policy=policy))
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
        app, host="0.0.0.0", port=8080, access_log=False, log_level="warning",
        lifespan="on", timeout_graceful_shutdown=5,
    ))
    signal.signal(signal.SIGTERM, lambda *_: setattr(server, "should_exit", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(server, "should_exit", True))
    server.run()


if __name__ == "__main__":
    main()
