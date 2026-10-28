"""Build and version information use case."""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class BuildInfo:
    """Build, contract, and runtime version manifest."""

    package_version: str
    contract_version: str
    python_version: str


def get_build_info(package_version: str, contract_version: str) -> BuildInfo:
    """Return the build/contract/runtime version manifest.

    Args:
        package_version: The AVA package version (from ``__version__``).
        contract_version: The API contract version (from openapi.yaml ``info.version``).
    """
    return BuildInfo(
        package_version=package_version,
        contract_version=contract_version,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
