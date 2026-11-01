import pytest

from porter.models import Status, TransitionError, check_transition, priority_from_labels, slugify


def test_slugify():
    assert slugify("Add a --version flag!") == "add-a-version-flag"
    assert slugify("???") == "task"
    assert len(slugify("x" * 100)) == 40


@pytest.mark.parametrize(
    ("labels", "expected"),
    [(["bug", "p2"], 2), (["P1", "p3"], 1), (["priority:p0"], 0), (["agent-ready"], 9)],
)
def test_priority_from_labels(labels, expected):
    assert priority_from_labels(labels) == expected


def test_transitions():
    check_transition(Status.QUEUED, Status.RUNNING)
    check_transition(Status.VERIFYING, Status.RUNNING)  # feedback retry
    check_transition(Status.NEEDS_HUMAN, Status.QUEUED)  # human resume
    with pytest.raises(TransitionError):
        check_transition(Status.QUEUED, Status.PR_OPEN)
    with pytest.raises(TransitionError):
        check_transition(Status.DONE, Status.QUEUED)
