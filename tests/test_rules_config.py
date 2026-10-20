"""E-2-T1 rule-profile configuration checks (no GPU, no model download).

Covers the acceptance criteria: valid profiles load; invalid examples fail
with actionable errors before a run starts (ADR-4).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ava.config import (
    CameraConfig,
    ClassFilter,
    DetectorConfig,
    DwellRule,
    EvidenceOptions,
    LineCrossingRule,
    LineGeometry,
    PolygonGeometry,
    RuleProfile,
    RunConfig,
    ZoneEntryRule,
    load_config,
)


def _detector() -> DetectorConfig:
    return DetectorConfig(kind="conventional", model=Path("models/yolo26n.pt"))


def _camera(profiles: list[str] | None = None) -> CameraConfig:
    return CameraConfig(
        id="cam-01", source=Path("videos/input.mp4"), profiles=profiles or []
    )


# --- valid profiles load -----------------------------------------------------


def test_valid_rule_profile_loads() -> None:
    profile = RuleProfile(
        id="crosswalk",
        rules=[
            LineCrossingRule(
                id="crossing",
                line=LineGeometry(start=(0.0, 0.5), end=(1.0, 0.5)),
                direction="both",
                classes=ClassFilter(include=["person"]),
                deadband=0.01,
                debounce_frames=2,
            ),
            ZoneEntryRule(
                id="zone",
                zone=PolygonGeometry(points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]),
            ),
            DwellRule(
                id="dwell",
                zone=PolygonGeometry(points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]),
                dwell_seconds=10.0,
                missing_track_grace_s=2.0,
            ),
        ],
    )
    assert [r.id for r in profile.rules] == ["crossing", "zone", "dwell"]


def test_e2_style_run_config_loads() -> None:
    cfg = RunConfig(
        source=Path("videos/input.mp4"),
        detector=_detector(),
        cameras=[_camera(["crosswalk"])],
        profiles=[
            RuleProfile(
                id="crosswalk",
                rules=[
                    LineCrossingRule(
                        id="c1", line=LineGeometry(start=(0.0, 0.5), end=(1.0, 0.5))
                    )
                ],
            )
        ],
    )
    assert cfg.schema_version == "1"
    assert cfg.cameras[0].profiles == ["crosswalk"]


def test_e2_config_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "run.yaml"
    p.write_text(
        "source: /videos/input.mp4\n"
        "detector:\n"
        "  kind: conventional\n"
        "  model: /models/yolo26n.pt\n"
        "cameras:\n"
        "  - id: cam-01\n"
        "    source: /videos/input.mp4\n"
        "    profiles: [crosswalk]\n"
        "profiles:\n"
        "  - id: crosswalk\n"
        "    rules:\n"
        "      - id: crossing\n"
        "        type: line_crossing\n"
        "        line:\n"
        "          start: [0.0, 0.5]\n"
        "          end: [1.0, 0.5]\n"
        "        direction: both\n"
        "        classes:\n"
        "          include: [person]\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert len(cfg.profiles) == 1
    rule = cfg.profiles[0].rules[0]
    assert isinstance(rule, LineCrossingRule)
    assert rule.classes.include == ["person"]


def test_e1_style_config_still_valid() -> None:
    # Backwards compatibility: no cameras/profiles section.
    cfg = RunConfig(source=Path("v.mp4"), detector=_detector())
    assert cfg.cameras == [] and cfg.profiles == []


# --- invalid examples fail with actionable errors ----------------------------


def test_line_endpoint_outside_frame_rejected() -> None:
    with pytest.raises(ValidationError, match="outside normalized"):
        LineGeometry(start=(0.0, 0.5), end=(1.2, 0.5))


def test_zero_length_line_rejected() -> None:
    with pytest.raises(ValidationError, match="zero-length"):
        LineGeometry(start=(0.5, 0.5), end=(0.5, 0.5))


def test_polygon_too_few_vertices_rejected() -> None:
    with pytest.raises(ValidationError):
        PolygonGeometry(points=[(0.1, 0.1), (0.9, 0.9)])


def test_polygon_duplicate_vertices_rejected() -> None:
    with pytest.raises(ValidationError, match="3 distinct"):
        PolygonGeometry(points=[(0.1, 0.1), (0.9, 0.1), (0.1, 0.1)])


def test_polygon_vertex_outside_frame_rejected() -> None:
    with pytest.raises(ValidationError, match="outside normalized"):
        PolygonGeometry(points=[(0.1, 0.1), (0.9, 0.1), (-0.2, 0.9)])


def test_dwell_seconds_must_be_positive() -> None:
    zone = PolygonGeometry(points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)])
    with pytest.raises(ValidationError):
        DwellRule(id="d", zone=zone, dwell_seconds=0.0)


def test_deadband_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        LineCrossingRule(
            id="c", line=LineGeometry(start=(0.0, 0.5), end=(1.0, 0.5)), deadband=1.5
        )


def test_negative_debounce_rejected() -> None:
    with pytest.raises(ValidationError):
        ZoneEntryRule(
            id="z",
            zone=PolygonGeometry(points=[(0.1, 0.1), (0.9, 0.1), (0.9, 0.9)]),
            debounce_frames=-1,
        )


def test_bad_rule_id_rejected() -> None:
    with pytest.raises(ValidationError, match="rule id"):
        LineCrossingRule(
            id="-bad id", line=LineGeometry(start=(0.0, 0.5), end=(1.0, 0.5))
        )


def test_duplicate_rule_ids_in_profile_rejected() -> None:
    line = LineGeometry(start=(0.0, 0.5), end=(1.0, 0.5))
    with pytest.raises(ValidationError, match="duplicate rule id"):
        RuleProfile(
            id="p",
            rules=[LineCrossingRule(id="same", line=line), LineCrossingRule(id="same", line=line)],
        )


def test_unresolved_profile_reference_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown rule profile"):
        RunConfig(
            source=Path("v.mp4"),
            detector=_detector(),
            cameras=[_camera(["missing-profile"])],
            profiles=[],
        )


def test_camera_run_without_detector_rejected() -> None:
    with pytest.raises(ValidationError, match="require a `detector`"):
        RunConfig(cameras=[_camera()], profiles=[])


def test_no_source_and_no_cameras_rejected() -> None:
    with pytest.raises(ValidationError, match="must define either"):
        RunConfig(detector=_detector())


def test_duplicate_camera_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate camera ids"):
        RunConfig(
            source=Path("v.mp4"),
            detector=_detector(),
            cameras=[_camera(), _camera()],
        )


def test_invalid_direction_rejected() -> None:
    with pytest.raises(ValidationError):
        LineCrossingRule(
            id="c",
            line=LineGeometry(start=(0.0, 0.5), end=(1.0, 0.5)),
            direction="diagonal",  # type: ignore[arg-type]
        )


def test_invalid_rule_type_rejected() -> None:
    with pytest.raises(ValidationError):
        RuleProfile(
            id="p",
            rules=[
                {
                    "id": "x",
                    "type": "motion",  # unknown rule type
                    "line": {"start": [0.0, 0.5], "end": [1.0, 0.5]},
                }
            ],
        )


def test_evidence_clip_seconds_positive() -> None:
    with pytest.raises(ValidationError):
        EvidenceOptions(clip=True, clip_seconds=0.0)
