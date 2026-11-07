"""Request contracts and reusable mutation behavior."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ava.api.app import create_app
from ava.application.mutation import (
    InvalidIfMatch,
    MutationAdmission,
    MutationResult,
    MutationScope,
    PreconditionFailed,
    TargetState,
    compute_fingerprint,
    etag_matches,
    execute_idempotent_mutation,
    make_etag,
    parse_if_match,
)

KEY = "abcdefab-0000-4000-8000-000000000001"


@pytest.mark.parametrize("path", ["/api/v1/version", "/api/v1/capabilities", "/api/v1/events"])
def test_reads_require_identification(path):
    with TestClient(create_app()) as client:
        response = client.get(path)
    assert response.status_code == 400
    assert response.json()["code"] == "idempotency_key_missing"
    assert response.json()["request_id"] == response.headers["X-Request-Id"]
    assert response.headers["X-Correlation-Id"]


@pytest.mark.parametrize("header", ["Idempotency-Key", "X-Correlation-Id"])
@pytest.mark.parametrize("bad", ["", "not-a-uuid", "a" * 32, KEY + "\n"])
def test_invalid_probe_identifiers_are_rejected(header, bad):
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/health/live", headers={header: bad})
    assert response.status_code == 400
    assert response.headers["X-Correlation-Id"]
    assert response.json()["request_id"] == response.headers["X-Request-Id"]


def test_uppercase_identifiers_normalize_and_log(caplog):
    with caplog.at_level(logging.INFO, logger="ava.api.request"), TestClient(create_app()) as client:
        response = client.get("/api/v1/health/live", headers={
            "Idempotency-Key": KEY.upper(), "X-Correlation-Id": KEY.upper()})
    assert response.status_code == 200
    assert response.headers["X-Correlation-Id"] == KEY
    assert f"idempotency_key={KEY}" in caplog.text
    assert f"correlation_id={KEY}" in caplog.text


@pytest.mark.parametrize("header,expected", [
    ('"one"', ['"one"']), ('"one", "two"', ['"one"', '"two"']),
    ('"a,b"', ['"a,b"']), ('W/"weak", "strong"', ['"strong"']),
    ('W/"weak"', []), ('""', ['""']), ('*', ['*']),
])
def test_if_match_parses_standard_tags(header, expected):
    assert parse_if_match(header) == expected


@pytest.mark.parametrize("header", ['', 'bare', '"a",', '*, "a"', '"a" junk', '"a\x01"'])
def test_if_match_rejects_malformed_syntax(header):
    with pytest.raises(InvalidIfMatch):
        parse_if_match(header)


@pytest.mark.parametrize("tags,current,expected", [
    (['*'], None, False), (['*'], '"current"', True),
    (['"other"', '"current"'], '"current"', True),
    ([], '"current"', False), (['"other"'], '"current"', False),
])
def test_strong_comparison(tags, current, expected):
    assert etag_matches(tags, current) is expected


def test_etags_distinguish_resource_identity_schema_and_revision():
    tuples = [("camera", "a", 1, 1), ("grant", "a", 1, 1),
              ("camera", "b", 1, 1), ("camera", "a", 2, 1), ("camera", "a", 1, 2)]
    tags = [make_etag(*values) for values in tuples]
    assert len(set(tags)) == len(tuples)
    assert all(tag.startswith('"') and tag.endswith('"') for tag in tags)


def test_fingerprints_include_method_target_body_and_precondition():
    base = compute_fingerprint("PUT", "/a", {"a": 1, "b": 2}, '"v"')
    assert base == compute_fingerprint("put", "/a", {"b": 2, "a": 1}, '"v"')
    assert base != compute_fingerprint("DELETE", "/a", {"a": 1, "b": 2}, '"v"')
    assert base != compute_fingerprint("PUT", "/b", {"a": 1, "b": 2}, '"v"')
    assert base != compute_fingerprint("PUT", "/a", {"a": 2}, '"v"')
    assert base != compute_fingerprint("PUT", "/a", {"a": 1, "b": 2}, '"w"')


def run_workflow(txn, **kwargs):
    return asyncio.run(execute_idempotent_mutation(
        txn=txn, namespace="caller", key_hash="key", fingerprint="fp",
        scope=MutationScope("camera:a"), camera_id="a", operation="grant_put",
        params={}, **kwargs))


def test_replay_releases_transaction_without_mutating():
    txn = AsyncMock()
    original = MutationResult(200, {"camera_id": "a"}, '"old"', True)
    txn.claim_key.return_value = (False, original)
    assert run_workflow(txn, if_match_raw='"old"') == original
    txn.rollback.assert_awaited_once()
    txn.apply_mutation.assert_not_awaited()


def test_false_precondition_rolls_back_claim():
    txn = AsyncMock()
    txn.claim_key.return_value = (True, None)
    txn.read_target.return_value = TargetState(False)
    with pytest.raises(PreconditionFailed):
        run_workflow(txn, if_match_raw='*')
    txn.rollback.assert_awaited_once()
    txn.apply_mutation.assert_not_awaited()


def test_cancellation_rolls_back_without_storing_result():
    txn = AsyncMock()
    txn.claim_key.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run_workflow(txn)
    txn.rollback.assert_awaited_once()
    txn.store_completed.assert_not_awaited()


def test_success_commits_result():
    txn = AsyncMock()
    txn.claim_key.return_value = (True, None)
    txn.read_target.return_value = TargetState(True, 1, '"v1"')
    txn.apply_mutation.return_value = TargetState(True, 2, '"v2"', {"camera_id": "a"})
    result = run_workflow(txn, if_match_raw='"v1"')
    assert result == MutationResult(200, {"camera_id": "a"}, '"v2"')
    txn.store_completed.assert_awaited_once_with("key", 200, {"camera_id": "a"}, '"v2"')
    txn.commit.assert_awaited_once()


def test_admission_rejects_excess_and_recovers():
    admission = MutationAdmission(1)
    assert admission.try_acquire()
    assert not admission.try_acquire()
    admission.release()
    assert admission.try_acquire()
    admission.release()
    admission.release()
    assert admission.active == 0
    with pytest.raises(ValueError):
        MutationAdmission(0)


def test_middleware_rejections_are_logged_with_request_id(caplog):
    with caplog.at_level(logging.INFO, logger="ava.api.request"), TestClient(create_app()) as client:
        response = client.get("/api/v1/version", headers={"X-Correlation-Id": "secret-malformed"})
    assert response.status_code == 400
    assert response.headers["X-Request-Id"] in caplog.text
    assert "secret-malformed" not in caplog.text
    assert "status=400" in caplog.text


def test_openapi_documents_identifiers_on_reads_and_optional_probes():
    from ava.api.schema import load_schema
    doc = load_schema()
    for path, item in doc["paths"].items():
        refs = [parameter["$ref"] for parameter in item["parameters"]]
        expected = "OptionalIdempotencyKey" if path.startswith("/health/") else "IdempotencyKey"
        assert f"#/components/parameters/{expected}" in refs
        assert "#/components/parameters/XCorrelationId" in refs
    assert doc["paths"]["/grants/{camera_id}"]["get"]["operationId"] == "getGrant"


def test_unexpected_errors_keep_identifiers_and_hide_internal_details(caplog):
    app = create_app()
    @app.get("/crash/{identifier}")
    def crash(identifier: str):
        raise RuntimeError("private-credential")
    with caplog.at_level(logging.INFO, logger="ava.api.request"), TestClient(app) as client:
        response = client.get("/crash/private-camera", headers={"Idempotency-Key": KEY})
    assert response.status_code == 500
    assert response.json()["request_id"] == response.headers["X-Request-Id"]
    assert response.headers["X-Correlation-Id"]
    assert "private-credential" not in response.text
    assert "private-camera" not in caplog.text
    assert "/crash/{identifier}" in caplog.text
