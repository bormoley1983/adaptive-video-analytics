"""Reject source or editable API imports before starting acceptance services."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import api_http_bootstrap as bootstrap
import pytest


def test_editable_api_install_is_rejected(monkeypatch):
    package = Mock()
    package.read_text.return_value = json.dumps({"dir_info": {"editable": True}})
    monkeypatch.setattr(bootstrap, "distribution", lambda name: package)

    with pytest.raises(RuntimeError, match="non-editable"):
        bootstrap.installed_api_provenance()


def test_source_checkout_import_is_rejected(tmp_path, monkeypatch):
    package = Mock()
    package.read_text.return_value = "{}"
    monkeypatch.setattr(bootstrap, "distribution", lambda name: package)
    monkeypatch.setattr(bootstrap, "import_module", lambda name: SimpleNamespace(
        __file__=str(tmp_path / "source" / "ava" / "__init__.py"),
    ))

    with pytest.raises(RuntimeError, match="source-checkout"):
        bootstrap.installed_api_provenance()
