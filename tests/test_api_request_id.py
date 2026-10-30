"""Unit tests for request-ID resolution and propagation.

Covers: generated ID when the header is absent, preserved valid UUID,
rejection of malformed values (invalid characters, wrong length, uppercase,
empty, oversized) with a fresh trace ID that never reflects the input,
problem responses carrying the request ID, log redaction, and concurrent
isolation.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from ava.api.app import AppConfig, create_app
from ava.api.request_id import (
    REQUEST_ID_HEADER,
    InvalidRequestIdError,
    generate_request_id,
    parse_request_id,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _client() -> TestClient:
    class Policy:
        def verify(self, token):
            from ava.application.identity import CallerIdentity

            return CallerIdentity(
                subject="test", tenant="test", roles=frozenset({"viewer"})
            )

    app = create_app(AppConfig(identity_policy=Policy()))
    return TestClient(app)


# ── parse_request_id unit behavior ───────────────────────────────────────────


class TestParseRequestId:
    def test_absent_header_generates_uuid(self):
        value = parse_request_id(None)
        assert _UUID_RE.match(value)

    def test_generated_ids_are_unique(self):
        values = {parse_request_id(None) for _ in range(50)}
        assert len(values) == 50

    def test_valid_lowercase_uuid_preserved(self):
        value = "3f2c1b6e-9a4d-4e5f-8b7c-1d2e3f4a5b6c"
        assert parse_request_id(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",  # empty
            "not-a-uuid",
            "3f2c1b6e9a4d4e5f8b7c1d2e3f4a5b6c",  # missing hyphens
            "3F2C1B6E-9A4D-4E5F-8B7C-1D2E3F4A5B6C",  # uppercase
            "3f2c1b6e-9a4d-4e5f-8b7c-1d2e3f4a5b6",  # one char short
            "3f2c1b6e-9a4d-4e5f-8b7c-1d2e3f4a5b6cd",  # one char long
            "3f2c1b6e_9a4d_4e5f_8b7c_1d2e3f4a5b6c",  # wrong separators
            "gghhiijj-kkll-mmmn-oooo-pppppppppppp",  # non-hex characters
            "3f2c1b6e-9a4d-4e5f-8b7c-1d2e3f4a5b6c\n",  # trailing newline
        ],
    )
    def test_invalid_values_raise_without_retaining_input(self, value: str):
        with pytest.raises(InvalidRequestIdError):
            parse_request_id(value)

    def test_error_carries_no_client_text(self):
        sentinel = "secret-malformed-value"
        try:
            parse_request_id(sentinel)
        except InvalidRequestIdError as exc:
            assert str(exc) == ""
            assert sentinel not in repr(exc)
        else:
            pytest.fail("expected InvalidRequestIdError")

    def test_generate_request_id_is_uuid(self):
        assert _UUID_RE.match(generate_request_id())


# ── Response header behavior ─────────────────────────────────────────────────


class TestResponseHeader:
    def test_generated_header_when_absent(self):
        client = _client()
        r = client.get("/api/v1/health/live")
        assert r.status_code == 200
        assert _UUID_RE.match(r.headers[REQUEST_ID_HEADER])

    def test_preserved_header_on_open_route(self):
        value = str(uuid.uuid4())
        client = _client()
        r = client.get("/api/v1/health/live", headers={REQUEST_ID_HEADER: value})
        assert r.status_code == 200
        assert r.headers[REQUEST_ID_HEADER] == value

    def test_preserved_header_on_protected_route(self):
        value = str(uuid.uuid4())
        client = _client()
        r = client.get(
            "/api/v1/version",
            headers={"Authorization": "Bearer t", REQUEST_ID_HEADER: value},
        )
        assert r.status_code == 200
        assert r.headers[REQUEST_ID_HEADER] == value

    def test_generated_header_on_protected_route(self):
        client = _client()
        r = client.get("/api/v1/version", headers={"Authorization": "Bearer t"})
        assert r.status_code == 200
        assert _UUID_RE.match(r.headers[REQUEST_ID_HEADER])


# ── Invalid header rejection ─────────────────────────────────────────────────


class TestInvalidHeaderRejection:
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not-a-uuid",
            "3F2C1B6E-9A4D-4E5F-8B7C-1D2E3F4A5B6C",  # uppercase
            "x" * 200,  # oversized
        ],
    )
    def test_malformed_value_rejected_with_400_problem(self, value: str):
        client = _client()
        r = client.get("/api/v1/health/live", headers={REQUEST_ID_HEADER: value})
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("application/problem+json")
        body = r.json()
        assert body["status"] == 400
        assert body["code"] == "invalid_request_id"

    def test_rejection_does_not_reflect_malformed_value(self):
        sentinel = "MALFORMED-SECRET-VALUE-should-not-appear"
        client = _client()
        r = client.get("/api/v1/health/live", headers={REQUEST_ID_HEADER: sentinel})
        assert r.status_code == 400
        raw = r.content.decode("utf-8")
        assert sentinel not in raw
        for name in r.headers:
            assert sentinel not in r.headers[name]

    def test_rejection_carries_fresh_generated_id(self):
        client = _client()
        r = client.get(
            "/api/v1/health/live", headers={REQUEST_ID_HEADER: "bogus"}
        )
        new_id = r.headers[REQUEST_ID_HEADER]
        assert _UUID_RE.match(new_id)
        assert r.json()["request_id"] == new_id

    def test_rejection_applies_before_authentication(self):
        """A malformed ID is rejected even on protected routes, before auth."""
        client = _client()
        r = client.get("/api/v1/version", headers={REQUEST_ID_HEADER: "bogus"})
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_request_id"


# ── Error responses carry the request ID ─────────────────────────────────────


class TestErrorResponsesCarryRequestId:
    def test_401_problem_body_contains_matching_request_id(self):
        value = str(uuid.uuid4())
        client = _client()
        r = client.get("/api/v1/version", headers={REQUEST_ID_HEADER: value})
        assert r.status_code == 401
        assert r.headers["content-type"].startswith("application/problem+json")
        assert r.json()["request_id"] == value
        assert r.headers[REQUEST_ID_HEADER] == value

    def test_401_problem_body_contains_generated_request_id(self):
        client = _client()
        r = client.get("/api/v1/version")
        assert r.status_code == 401
        new_id = r.headers[REQUEST_ID_HEADER]
        assert _UUID_RE.match(new_id)
        assert r.json()["request_id"] == new_id


# ── Log redaction ────────────────────────────────────────────────────────────


class TestLogRedaction:
    def test_malformed_value_never_logged(self, caplog: pytest.LogCaptureFixture):
        sentinel = "MALFORMED-SECRET-VALUE-should-not-appear"
        client = _client()
        with caplog.at_level(logging.INFO, logger="ava.api.request"):
            r = client.get(
                "/api/v1/health/live", headers={REQUEST_ID_HEADER: sentinel}
            )
        assert r.status_code == 400
        for record in caplog.records:
            assert sentinel not in record.getMessage()

    def test_valid_id_appears_in_log_line(self, caplog: pytest.LogCaptureFixture):
        value = str(uuid.uuid4())
        client = _client()
        with caplog.at_level(logging.INFO, logger="ava.api.request"):
            r = client.get(
                "/api/v1/health/live", headers={REQUEST_ID_HEADER: value}
            )
        assert r.status_code == 200
        lines = [rec.getMessage() for rec in caplog.records]
        assert any(f"request_id={value}" in line for line in lines)


# ── Concurrent isolation ─────────────────────────────────────────────────────


class TestConcurrentIsolation:
    def test_concurrent_requests_get_distinct_ids(self):
        client = _client()
        ids: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                r = client.get("/api/v1/health/live")
                assert r.status_code == 200
                with lock:
                    ids.append(r.headers[REQUEST_ID_HEADER])
            except BaseException as exc:  # noqa: BLE001 - report any failure
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(ids) == 20
        assert len(set(ids)) == 20

    def test_concurrent_supplied_ids_are_preserved_per_request(self):
        client = _client()
        supplied = [str(uuid.uuid4()) for _ in range(10)]
        seen: dict[str, str] = {}
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(value: str) -> None:
            try:
                r = client.get(
                    "/api/v1/health/live", headers={REQUEST_ID_HEADER: value}
                )
                assert r.status_code == 200
                with lock:
                    seen[value] = r.headers[REQUEST_ID_HEADER]
            except BaseException as exc:  # noqa: BLE001 - report any failure
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(v,)) for v in supplied]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert seen == dict(zip(supplied, supplied))


# ── OpenAPI contract documents the header ────────────────────────────────────


class TestOpenApiContract:
    def test_header_component_and_responses_documented(self):
        from ava.api.schema import load_schema

        doc = load_schema()
        headers = doc.get("components", {}).get("headers", {})
        assert "XRequestId" in headers
        for path in ("/health/live", "/health/ready", "/version", "/capabilities"):
            responses = doc["paths"][path]["get"]["responses"]
            assert "200" in responses
            assert (
                responses["200"].get("headers", {}).get("X-Request-Id") is not None
            )
        for path in ("/version", "/capabilities"):
            assert "400" in doc["paths"][path]["get"]["responses"]
