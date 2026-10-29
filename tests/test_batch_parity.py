import importlib.util
from pathlib import Path

import pytest

from ava.detector import Detection

spec = importlib.util.spec_from_file_location("parity", Path(__file__).parents[1] /
                                             "benchmarks/e-3/python_batch_parity.py")
parity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parity)


def det(score=.9, class_id=0, box=(0., 0., 1., 1.)):
    return Detection(class_id, "person", score, box)


def test_parity_requires_class_count_iou_and_confidence():
    assert parity.compare([det()], [det()])["passed"]
    assert parity.compare([], [])["passed"]
    for other in ([], [det(class_id=1)], [det(score=.89)], [det(box=(.5, .5, 1., 1.))]):
        assert not parity.compare([det()], other)["passed"]


def test_matching_can_reassign_ambiguous_boxes():
    reference = [det(.9), det(.9015)]
    candidate = [det(.9006), det(.8995)]
    assert parity.compare(reference, candidate)["passed"]


@pytest.mark.integration
def test_real_synthetic_event_trace_has_deterministic_positive_and_negative_cases():
    trace = parity.synthetic_events()
    assert trace == parity.synthetic_events()
    assert len(trace["positive"]) == 2
    assert trace["negative"] == []
