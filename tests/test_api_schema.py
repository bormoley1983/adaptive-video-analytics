"""Unit tests for the AVA API OpenAPI schema and common types."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from ava.api.schema import (
    Binding,
    CameraId,
    Confidence,
    ConfidenceKind,
    EventId,
    FieldError,
    Geometry,
    GeometryKind,
    Job,
    JobFailure,
    JobState,
    NormalizedPoint,
    PageQuery,
    PageResponse,
    ProblemDetail,
    ResourceEnvelope,
    SchemaVersion,
    SiteId,
    SourceTime,
    load_schema,
)

OPENAPI_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"


# ── Gate 1: OpenAPI document parses as valid 3.1 ──────────────────────────────


class TestOpenAPIDocument:
    def test_loads_as_openapi_3_1(self):
        doc = load_schema(OPENAPI_PATH)
        assert doc["openapi"].startswith("3.1")

    def test_has_info_title_and_version(self):
        doc = load_schema(OPENAPI_PATH)
        assert "title" in doc["info"]
        assert "version" in doc["info"]

    def test_has_components_schemas(self):
        doc = load_schema(OPENAPI_PATH)
        schemas = doc["components"]["schemas"]
        expected = {
            "SchemaVersion",
            "EventId",
            "CameraId",
            "SiteId",
            "SessionId",
            "EvidenceId",
            "TenantId",
            "JobId",
            "MessageId",
            "ProfileId",
            "IncidentId",
            "MediaAssetId",
            "SearchId",
            "DeliverySubscriptionId",
            "WorkerId",
            "Instant",
            "SourceTime",
            "NormalizedPoint",
            "NormalizedBox",
            "Geometry",
            "Confidence",
            "PageQuery",
            "PageResponse",
            "JobState",
            "JobFailure",
            "Job",
            "ProblemDetail",
            "FieldError",
            "ResourceEnvelope",
            "Binding",
        }
        assert expected.issubset(schemas.keys())

    def test_paths_is_empty(self):
        doc = load_schema(OPENAPI_PATH)
        assert doc.get("paths") == {}

    def test_rejects_nonexistent_file(self, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            load_schema(tmp_path / "nope.yaml")

    def test_rejects_non_31_document(self, tmp_path):
        p = tmp_path / "old.yaml"
        p.write_text("openapi: '3.0.0'\ninfo:\n  title: x\n  version: '1'\n")
        with pytest.raises(ValueError, match="Expected OpenAPI 3.1"):
            load_schema(p)

    def test_rejects_non_mapping(self, tmp_path):
        p = tmp_path / "list.yaml"
        p.write_text("- a\n- b\n")
        with pytest.raises(TypeError, match="not a mapping"):
            load_schema(p)


# ── Gate 2: Documented examples validate against their schemas ────────────────


class TestExamplesValidate:
    """Every `examples:` entry in the YAML must conform to its schema."""

    def test_all_examples_are_valid(self):
        doc = load_schema(OPENAPI_PATH)
        schemas = doc["components"]["schemas"]
        checked = 0
        for name, schema in schemas.items():
            if "examples" not in schema:
                continue
            for example in schema["examples"]:
                _validate_against_schema(example, schema, f"{name}.example={example!r}")
                checked += 1
        assert checked > 0, "no examples found in the document"

    def test_event_id_example_matches_pattern(self):
        doc = load_schema(OPENAPI_PATH)
        pattern = doc["components"]["schemas"]["EventId"]["pattern"]
        example = doc["components"]["schemas"]["EventId"]["examples"][0]
        import re

        assert re.match(pattern, example)

    def test_instant_example_is_rfc3339_with_fractional_seconds(self):
        doc = load_schema(OPENAPI_PATH)
        example = doc["components"]["schemas"]["Instant"]["examples"][0]
        # Must have fractional seconds: .NNN before Z
        import re

        assert re.search(r"\.\d{1,6}Z$", example)


def _validate_against_schema(value, schema: dict, label: str):
    """Minimal JSON-Schema validation for the subset we use in openapi.yaml."""
    stype = schema.get("type")
    if stype == "string":
        assert isinstance(value, str), f"{label}: expected string"
        if "pattern" in schema:
            import re

            assert re.match(schema["pattern"], value), f"{label}: pattern mismatch"
    elif stype == "integer":
        assert isinstance(value, int) and not isinstance(value, bool), f"{label}"
        if "minimum" in schema:
            assert value >= schema["minimum"], f"{label}: below minimum"
    elif stype == "number":
        assert isinstance(value, (int, float)), f"{label}"
        if "minimum" in schema:
            assert value >= schema["minimum"], f"{label}: below minimum"
        if "maximum" in schema:
            assert value <= schema["maximum"], f"{label}: above maximum"
    elif stype == "boolean":
        assert isinstance(value, bool), f"{label}"
    elif stype == "array":
        assert isinstance(value, list), f"{label}: expected array"
        if "minItems" in schema:
            assert len(value) >= schema["minItems"], f"{label}: too few items"
        if "maxItems" in schema:
            assert len(value) <= schema["maxItems"], f"{label}: too many items"
    elif stype == "object":
        assert isinstance(value, dict), f"{label}: expected object"
        for req in schema.get("required", []):
            assert req in value, f"{label}: missing required field {req!r}"


# ── Gate 3: Pydantic models round-trip with the YAML schemas ──────────────────


class TestPydanticModels:
    def test_source_time_valid(self):
        st = SourceTime(
            offset_seconds=184.233,
            captured_at="2026-10-21T09:15:31.233Z",
            clock_kind="camera_utc",
            uncertainty_ms=120,
            synthetic=False,
        )
        assert st.offset_seconds == 184.233
        assert st.clock_kind == "camera_utc"

    def test_source_time_rejects_negative_offset(self):
        with pytest.raises(ValidationError):
            SourceTime(offset_seconds=-1)

    def test_geometry_box_valid(self):
        g = Geometry(kind="box", coordinates=[0.41, 0.22, 0.55, 0.94])
        assert g.kind == GeometryKind.BOX

    def test_geometry_polygon_valid(self):
        pts = [
            NormalizedPoint(x=0.1, y=0.1),
            NormalizedPoint(x=0.9, y=0.1),
            NormalizedPoint(x=0.5, y=0.9),
        ]
        g = Geometry(kind="polygon", coordinates=pts)
        assert g.kind == GeometryKind.POLYGON

    def test_geometry_box_rejects_out_of_range(self):
        with pytest.raises(ValidationError):
            Geometry(kind="box", coordinates=[-0.1, 0.22, 0.55, 0.94])

    def test_confidence_valid(self):
        c = Confidence(value=0.91, kind="detector")
        assert c.kind == ConfidenceKind.DETECTOR

    def test_confidence_rejects_above_one(self):
        with pytest.raises(ValidationError):
            Confidence(value=1.5, kind="rule")

    def test_job_valid(self):
        j = Job(
            id="job_71HFE865V215DE1CTEWH0AVNH9",
            state="running",
            created_at="2026-10-21T09:15:31.233Z",
            updated_at="2026-10-21T09:15:32.000Z",
            progress=0.5,
        )
        assert j.state == JobState.RUNNING

    def test_job_failure_valid(self):
        f = JobFailure(code="source_unreachable", message="no response")
        assert f.code == "source_unreachable"

    def test_problem_detail_valid(self):
        p = ProblemDetail(
            type="https://ava.example.com/problems/validation_error",
            title="Validation error",
            status=422,
            code="validation_error",
            detail="field out of range",
            request_id="req_01J9ABCD1234EFGH56789KLMNP",
            errors=[FieldError(field="x.y", message="must be >= 0")],
        )
        assert p.status == 422
        assert len(p.errors) == 1

    def test_problem_detail_rejects_bad_status(self):
        with pytest.raises(ValidationError):
            ProblemDetail(type="t", title="t", status=99, code="c")

    def test_resource_envelope_valid(self):
        r = ResourceEnvelope(
            schema_version="1.0",
            id="evt_01J9ABCD1234EFGH56789KLMNP",
            created_at="2026-10-21T09:15:31.233Z",
        )
        assert r.schema_version == SchemaVersion.V1_0

    def test_binding_valid(self):
        b = Binding(system="example-consumer", kind="camera", external_id="ext-1")
        assert b.system == "example-consumer"

    def test_page_query_defaults(self):
        q = PageQuery()
        assert q.page_size == 50
        assert q.page_cursor is None

    def test_page_response_valid(self):
        r = PageResponse(items=[{"id": "evt_01J9ABCD1234EFGH56789KLMNP"}], next_cursor=None)
        assert r.next_cursor is None

    def test_event_id_valid(self):
        from pydantic import TypeAdapter

        adapter = TypeAdapter(EventId)
        assert adapter.validate_python("evt_71HFE865V215DE1CTEWH0AVNH9") == "evt_71HFE865V215DE1CTEWH0AVNH9"

    def test_event_id_rejects_wrong_prefix(self):
        from pydantic import TypeAdapter

        adapter = TypeAdapter(EventId)
        with pytest.raises(ValidationError):
            adapter.validate_python("cam_71HFE865V215DE1CTEWH0AVNH9")

    def test_camera_id_valid(self):
        from pydantic import TypeAdapter

        adapter = TypeAdapter(CameraId)
        assert adapter.validate_python("cam_71HFE865V215DE1CTEWH0AVNH9") == "cam_71HFE865V215DE1CTEWH0AVNH9"

    def test_site_id_rejects_bad_body(self):
        from pydantic import TypeAdapter

        adapter = TypeAdapter(SiteId)
        with pytest.raises(ValidationError):
            adapter.validate_python("site_not-a-ulid-at-all!")


# ── Gate 4: Breaking-change detection ─────────────────────────────────────────


class TestBreakingChangeDetection:
    """Verify that breaking changes in the schema document are detected.

    Uses api-schema-diff for the full document and a structural diff for
    component-level schema changes (required fields, enum values) that
    api-schema-diff does not cover when no paths are defined.
    """

    @pytest.fixture()
    def base_doc(self):
        return load_schema(OPENAPI_PATH)

    def test_removing_a_required_field_is_breaking(self, base_doc):
        broken = copy.deepcopy(base_doc)
        broken["components"]["schemas"]["ProblemDetail"]["required"] = [
            "type",
            "title",
            "status",
        ]
        diff = _structural_diff(base_doc, broken)
        breaking = [d for d in diff if d["breaking"]]
        assert breaking, "removing a required field should be breaking"

    def test_adding_an_optional_field_is_not_breaking(self, base_doc):
        additive = copy.deepcopy(base_doc)
        additive["components"]["schemas"]["ProblemDetail"]["properties"][
            "trace_id"
        ] = {"type": "string"}
        diff = _structural_diff(base_doc, additive)
        breaking = [d for d in diff if d["breaking"]]
        assert not breaking, f"additive change flagged as breaking: {breaking}"

    def test_changing_enum_values_is_breaking(self, base_doc):
        broken = copy.deepcopy(base_doc)
        broken["components"]["schemas"]["JobState"]["enum"] = [
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelling",
            "cancelled",
        ]
        diff = _structural_diff(base_doc, broken)
        breaking = [d for d in diff if d["breaking"]]
        assert breaking, "removing an enum value should be breaking"

    def test_unchanged_document_has_no_diff(self, base_doc):
        diff = _structural_diff(base_doc, copy.deepcopy(base_doc))
        assert not diff

    def test_api_schema_diff_runs_on_document(self, base_doc):
        """api-schema-diff can process the document without error."""
        from schema_diff.diff import diff_objects

        result = diff_objects(base_doc, copy.deepcopy(base_doc))
        assert not result.breaking


def _structural_diff(base: dict, candidate: dict) -> list[dict]:
    """Structural diff of components/schemas for breaking-change detection.

    Covers the change classes that api-schema-diff misses when no paths
    are defined: removed required fields, removed enum values, type changes.
    """
    changes = []
    base_schemas = base.get("components", {}).get("schemas", {})
    cand_schemas = candidate.get("components", {}).get("schemas", {})

    for name in base_schemas:
        if name not in cand_schemas:
            changes.append({"path": f"schemas.{name}", "kind": "removed", "breaking": True})
            continue
        b = base_schemas[name]
        c = cand_schemas[name]

        for field in set(b.get("required", [])) - set(c.get("required", [])):
            changes.append({
                "path": f"schemas.{name}.required.{field}",
                "kind": "removed",
                "breaking": True,
            })

        if "enum" in b and "enum" in c:
            for val in set(b["enum"]) - set(c["enum"]):
                changes.append({
                    "path": f"schemas.{name}.enum.{val}",
                    "kind": "removed",
                    "breaking": True,
                })

        if b.get("type") != c.get("type"):
            changes.append({
                "path": f"schemas.{name}.type",
                "kind": "changed",
                "breaking": True,
            })

    for name in cand_schemas:
        if name not in base_schemas:
            changes.append({"path": f"schemas.{name}", "kind": "added", "breaking": False})

    return changes
