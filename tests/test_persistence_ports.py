"""Unit tests for persistence ports and adapters."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ava.config import PersistenceConfig
from ava.events import EventRecord, EvidenceLocator
from ava.persistence.compose import build_event_sink
from ava.persistence.jsonl_sink import JsonlEventSink
from ava.persistence.ports import EventSink


def _make_record(event_id: str = "cam-01/s1/track-1/rule-1/crossing/1") -> EventRecord:
    """Build a minimal valid EventRecord for testing."""
    return EventRecord(
        schema_version="1",
        event_id=event_id,
        camera_id="cam-01",
        session_id="s1",
        track_id=1,
        rule_id="rule-1",
        rule_type="line_crossing",
        class_label="person",
        confidence=0.92,
        source_time_s=12.5,
        emitted_utc="2026-10-27T12:00:00.000Z",
        config_version="1",
        model_ref="models/yolo26n.pt",
        evidence=EvidenceLocator(kind="snapshot", path="evidence/cam-01/test.jpg"),
        frame_index=42,
        capture_utc="2026-10-27T12:00:00.000Z",
        time_basis="live_epoch",
        payload={"direction": "left_to_right"},
    )


class TestEventSinkProtocol:
    """Verify that adapters structurally satisfy the EventSink protocol."""

    def test_jsonl_sink_satisfies_event_sink_protocol(self) -> None:
        sink = JsonlEventSink(Path("/tmp/should-not-exist.jsonl"))
        assert isinstance(sink, EventSink)
        sink.close()

    def test_jsonl_sink_has_write_and_close(self) -> None:
        sink = JsonlEventSink(Path("/tmp/should-not-exist.jsonl"))
        assert callable(getattr(sink, "write", None))
        assert callable(getattr(sink, "close", None))
        sink.close()


class TestJsonlEventSink:
    """Behavioral tests for the JSONL adapter."""

    def test_write_returns_true_for_new_record(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonlEventSink(path)
        record = _make_record()
        result = sink.write(record)
        assert result is True
        sink.close()
        # Verify the file contains the record.
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event_id"] == record.event_id

    def test_write_appends_multiple_records(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonlEventSink(path)
        for i in range(3):
            record = _make_record(event_id=f"cam-01/s1/track-1/rule-1/crossing/{i}")
            assert sink.write(record) is True
        sink.close()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_write_without_dedupe_appends_duplicates(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonlEventSink(path)
        record = _make_record()
        assert sink.write(record) is True
        # Without dedupe, the same record is appended again.
        assert sink.write(record) is True
        sink.close()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_write_with_dedupe_skips_duplicates(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonlEventSink(path, dedupe=True)
        record = _make_record()
        assert sink.write(record) is True
        # Duplicate is skipped.
        assert sink.write(record) is False
        sink.close()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1

    def test_write_with_dedupe_rebuilds_seen_set_from_existing_file(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "events.jsonl"
        # Pre-existing file with one record.
        record1 = _make_record(event_id="existing-id")
        sink1 = JsonlEventSink(path)
        sink1.write(record1)
        sink1.close()

        # New sink with dedupe should skip the existing ID.
        sink2 = JsonlEventSink(path, dedupe=True)
        assert sink2.write(record1) is False
        # A new ID is accepted.
        record2 = _make_record(event_id="new-id")
        assert sink2.write(record2) is True
        sink2.close()

    def test_close_closes_file_handle(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        sink = JsonlEventSink(path)
        sink.close()
        # Writing after close should raise.
        with pytest.raises(ValueError):
            sink.write(_make_record())


class TestBuildEventSink:
    """Tests for the composition root."""

    def test_jsonl_backend_returns_jsonl_sink(self, tmp_path: Path) -> None:
        config = PersistenceConfig(backend="jsonl")
        path = tmp_path / "events.jsonl"
        sink = build_event_sink(config, jsonl_path=path)
        assert isinstance(sink, JsonlEventSink)
        sink.close()

    def test_jsonl_backend_without_path_raises(self) -> None:
        config = PersistenceConfig(backend="jsonl")
        with pytest.raises(ValueError, match="jsonl_path is required"):
            build_event_sink(config)

    @patch("ava.persistence.postgres.PostgresEventSink")
    def test_postgres_backend_returns_postgres_sink(self, mock_cls: MagicMock) -> None:
        config = PersistenceConfig(backend="postgres", dsn="postgresql://test")
        sink = build_event_sink(config)
        # The composition root should have called PostgresEventSink with the DSN.
        mock_cls.assert_called_once_with("postgresql://test")
        # The returned object is the mock's return value.
        assert sink is mock_cls.return_value


class TestPersistenceConfig:
    """Validation tests for the persistence config model."""

    def test_default_backend_is_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitted persistence defaults to postgres; DSN checked at sink build."""
        monkeypatch.delenv("AVA_PG_DSN", raising=False)
        config = PersistenceConfig()
        assert config.backend == "postgres"
        assert config.dsn is None

    def test_postgres_without_dsn_raises_at_sink_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DSN is enforced when the sink is constructed, not at config load."""
        monkeypatch.delenv("AVA_PG_DSN", raising=False)
        config = PersistenceConfig(backend="postgres")
        assert config.dsn is None
        from ava.persistence.compose import build_event_sink

        with pytest.raises(ValueError, match="dsn is required"):
            build_event_sink(config)

    def test_postgres_with_dsn_succeeds(self) -> None:
        config = PersistenceConfig(
            backend="postgres", dsn="postgresql://ava:pass@localhost/ava"
        )
        assert config.backend == "postgres"
        assert config.dsn is not None

    def test_jsonl_with_dsn_is_ignored(self) -> None:
        config = PersistenceConfig(backend="jsonl", dsn="postgresql://x")
        assert config.backend == "jsonl"

    # ------------------------------------------------------------------
    # Regression: default-backend policy (breaking change)
    # ------------------------------------------------------------------

    def test_omitted_persistence_requires_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitted persistence defaults to postgres; DSN checked at sink build."""
        monkeypatch.delenv("AVA_PG_DSN", raising=False)
        # Config loads fine without a DSN (detection-only runs don't need one).
        config = PersistenceConfig()
        assert config.backend == "postgres"
        assert config.dsn is None
        # But constructing the sink raises.
        from ava.persistence.compose import build_event_sink

        with pytest.raises(ValueError, match="dsn is required"):
            build_event_sink(config)

    def test_omitted_persistence_with_env_dsn_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With AVA_PG_DSN set, omitted persistence resolves to postgres."""
        monkeypatch.setenv("AVA_PG_DSN", "postgresql://ava:***@localhost/ava")
        config = PersistenceConfig()
        assert config.backend == "postgres"
        assert config.dsn is not None
        assert "localhost" in config.dsn

    def test_explicit_jsonl_does_not_require_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit jsonl backend works without any DSN."""
        monkeypatch.delenv("AVA_PG_DSN", raising=False)
        config = PersistenceConfig(backend="jsonl")
        assert config.backend == "jsonl"
        assert config.dsn is None
