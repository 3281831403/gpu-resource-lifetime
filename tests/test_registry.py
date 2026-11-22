"""Contract tests for :mod:`lifetime.registry`.

These tests pin down the three numbers that make deferred release safe:

* the reference count must never go negative and a double release is a no-op;
* a zero-reference resource survives its release frame plus
  ``RELEASE_DELAY_FRAMES`` more frames, then is reclaimed on the first tick
  whose frame is strictly past the deadline;
* a recycled slot always carries a new generation, so an old handle can never
  address the new resource.

The registry is a plain dict whose layout is documented in
``lifetime/registry.py``.  Tests that exercise the pure auditors
(``cycle_detect``, ``validate_registry``) build that dict literally; the
mutation tests drive the public API and only read documented keys.

Every test calls at least one function from ``lifetime.registry``.  The
initial state of the exercise is "all stubs raise NotImplementedError", so
the whole file is red until the contract is implemented.  ``tests/`` is not
modifiable.
"""

from __future__ import annotations

import pytest

from lifetime import registry as reg

SLOT_BITS = reg.SLOT_BITS
SLOT_MASK = reg.SLOT_MASK


def h(slot: int, generation: int) -> int:
    """Build a handle with the documented ``(generation << SLOT_BITS) | slot`` layout."""
    return (generation << SLOT_BITS) | slot


def _live(
    desc: str,
    handle: int,
    *,
    refcount: int = 1,
    created: int = 0,
    last_used: int | None = None,
    edges: tuple[int, ...] = (),
    released: int | None = None,
    deadline: int | None = None,
) -> dict:
    """Build one resource record in the documented shape."""
    if last_used is None:
        last_used = created
    return {
        "handle": handle,
        "desc": desc,
        "refcount": refcount,
        "created_frame": created,
        "last_used_frame": last_used,
        "edges": list(edges),
        "released_frame": released,
        "deadline": deadline,
    }


def _registry(*records: dict, frame: int = 0, pending=None, free_slots=()) -> dict:
    """Build a registry dict in the documented shape."""
    resources: dict[int, dict] = {}
    generations: dict[int, int] = {}
    for record in records:
        handle = record["handle"]
        slot = handle & SLOT_MASK
        resources[slot] = record
        generations[slot] = handle >> SLOT_BITS
    return {
        "frame": frame,
        "next_slot": max(generations, default=-1) + 1,
        "free_slots": list(free_slots),
        "generations": generations,
        "resources": resources,
        "pending_free": dict(pending or {}),
    }


def _new_handle(registry: dict, desc: str) -> int:
    """Allocate a resource through the public API and return its integer handle."""
    return reg.create(registry, desc)["handle"]


# ---------------------------------------------------------------------------
# One test per planted defect (1-10).
# ---------------------------------------------------------------------------


def test_release_defers_reclaim_until_after_grace_window():
    """Defect 1: release must park a zero-reference resource, never free it inline."""
    registry = _registry()
    handle = _new_handle(registry, "hot-tex")

    snapshot = reg.release(registry, handle, 10)
    assert snapshot["refcount"] == 0
    assert snapshot["released_frame"] == 10
    assert snapshot["deadline"] == 10 + reg.RELEASE_DELAY_FRAMES
    # The resource is queued, not gone: same-frame users still hold a valid handle.
    assert handle in registry["pending_free"]
    assert (handle & SLOT_MASK) in registry["resources"]

    reg.tick(registry, 11)
    reg.tick(registry, 12)
    assert (handle & SLOT_MASK) in registry["resources"]

    outcome = reg.tick(registry, 13)
    assert outcome["released"] == [handle]
    assert (handle & SLOT_MASK) not in registry["resources"]


def test_tick_keeps_resources_alive_on_deadline_frame():
    """Defect 2: the deadline frame itself is still inside the grace window."""
    registry = _registry()
    early = _new_handle(registry, "early-tex")
    late = _new_handle(registry, "late-tex")

    reg.release(registry, early, 10)  # deadline 12
    reg.release(registry, late, 11)  # deadline 13

    reg.tick(registry, 12)
    assert (early & SLOT_MASK) in registry["resources"]
    assert (late & SLOT_MASK) in registry["resources"]

    outcome = reg.tick(registry, 13)
    assert outcome["released"] == [early]
    assert (early & SLOT_MASK) not in registry["resources"]
    assert (late & SLOT_MASK) in registry["resources"]

    assert reg.tick(registry, 14)["released"] == [late]


def test_recycled_slot_bumps_generation_and_detects_old_handle():
    """Defect 3: reuse compares generations, not just the slot number."""
    registry = _registry()
    old = _new_handle(registry, "old-tex")
    reg.release(registry, old, 1)
    reg.tick(registry, 4)
    assert (old & SLOT_MASK) not in registry["resources"]

    new = _new_handle(registry, "new-tex")
    assert (new & SLOT_MASK) == (old & SLOT_MASK)
    assert new != old
    assert reg.reuse_conflict(registry, old) is True
    assert reg.reuse_conflict(registry, new) is False

    with pytest.raises(KeyError):
        reg.retain(registry, old, 5)


def test_retain_refreshes_last_used_frame():
    """Defect 4: a retain in the current frame must keep the resource out of the leak report."""
    registry = _registry()
    handle = _new_handle(registry, "active-tex")
    assert registry["frame"] == 0

    reg.tick(registry, 700)
    reg.retain(registry, handle, 700)
    reg.tick(registry, 750)

    assert reg.leak_report(registry) == []


def test_leak_report_ignores_deferred_zero_refcount_resource():
    """Defect 5: zero refcount + pending queue is deferred, not leaked (and never duplicated)."""
    registry = _registry()
    handle = _new_handle(registry, "deferred-tex")

    reg.release(registry, handle, 100)  # deadline 102
    reg.tick(registry, 101)

    report = reg.leak_report(registry)
    assert report == []
    assert not any("deferred-tex" in problem for problem in report)

    reg.tick(registry, 103)
    assert (handle & SLOT_MASK) not in registry["resources"]
    assert reg.leak_report(registry) == []


def test_cycle_detect_reports_multi_node_cycle():
    """Defect 6: cycles longer than a self loop must be reported too."""
    a, b, c, d = h(0, 1), h(1, 1), h(2, 1), h(3, 1)
    registry = _registry(
        _live("a-tex", a, edges=(b,)),
        _live("b-tex", b, edges=(c,)),
        _live("c-tex", c, edges=(a,)),
        _live("d-tex", d, edges=(d,)),
    )

    assert reg.cycle_detect(registry) == [
        ["a-tex", "b-tex", "c-tex"],
        ["d-tex"],
    ]


def test_validate_registry_reports_dangling_pending_free():
    """Defect 7: a queue entry whose live record vanished must be flagged."""
    dangling = h(0, 1)
    registry = _registry(frame=20, pending={dangling: 21})
    assert registry["resources"] == {}

    problems = reg.validate_registry(registry)
    assert problems
    assert any(str(dangling) in problem for problem in problems)


def test_release_suppresses_negative_refcount():
    """Defect 8: releasing a zero-reference resource must not drive the count to -1."""
    registry = _registry()
    handle = _new_handle(registry, "count-tex")

    assert reg.release(registry, handle, 5)["refcount"] == 0
    second = reg.release(registry, handle, 6)
    assert second["refcount"] == 0
    assert second["released_frame"] == 5
    assert second["deadline"] == 7
    assert len(registry["pending_free"]) == 1

    assert reg.tick(registry, 8)["released"] == [handle]


def test_handle_slot_space_is_recycled_after_a_million_cycles():
    """Defect 9: slots must be recycled instead of growing a monotonically increasing id."""
    registry = _registry()

    for _ in range(1_000_000):
        current = registry["frame"]
        handle = _new_handle(registry, "churn-tex")
        reg.release(registry, handle, current)
        reg.tick(registry, current + 3)

    assert registry["resources"] == {}
    assert registry["next_slot"] <= 8
    assert registry["generations"][0] == 1_000_000

    handle = _new_handle(registry, "after-churn-tex")
    assert (handle & SLOT_MASK) == 0
    assert reg.retain(registry, handle, registry["frame"])["refcount"] == 2


def test_tick_reclaims_all_expired_resources_in_one_pass():
    """Defect 10: one tick must drain the whole expired backlog."""
    registry = _registry()
    handles = [_new_handle(registry, f"batch-{index}") for index in range(3)]
    for handle in handles:
        reg.release(registry, handle, 10)

    outcome = reg.tick(registry, 13)
    assert sorted(outcome["released"]) == sorted(handles)
    assert registry["resources"] == {}
    assert reg.tick(registry, 13)["released"] == []


# ---------------------------------------------------------------------------
# Five additional contract tests (deferred usability, exact boundary,
# double release, long churn, stale handle).
# ---------------------------------------------------------------------------


def test_zero_refcount_resource_still_usable_in_same_frame():
    """A zero-reference resource inside the grace window can be retained again."""
    registry = _registry()
    handle = _new_handle(registry, "same-frame-tex")

    assert reg.release(registry, handle, 20)["refcount"] == 0
    revived = reg.retain(registry, handle, 20)
    assert revived["refcount"] == 1
    assert revived["released_frame"] is None
    assert revived["deadline"] is None
    assert handle not in registry["pending_free"]

    reg.tick(registry, 25)
    assert (handle & SLOT_MASK) in registry["resources"]


def test_delayed_reclaim_happens_exactly_on_the_next_frame():
    """One frame neither early nor late: deadline frame alive, next frame reclaimed once."""
    registry = _registry()
    handle = _new_handle(registry, "exact-tex")
    reg.release(registry, handle, 10)  # deadline 12

    assert reg.tick(registry, 11)["released"] == []
    assert reg.tick(registry, 12)["released"] == []
    assert reg.tick(registry, 12)["released"] == []
    assert (handle & SLOT_MASK) in registry["resources"]

    assert reg.tick(registry, 13)["released"] == [handle]
    assert registry["pending_free"] == {}


def test_double_release_does_not_change_the_count():
    """The second release of the same reference is suppressed, not counted."""
    registry = _registry()
    handle = _new_handle(registry, "double-tex")
    reg.retain(registry, handle, 1)  # refcount 2
    reg.release(registry, handle, 2)  # refcount 1
    reg.release(registry, handle, 3)  # refcount 0, deadline 5

    extra = reg.release(registry, handle, 4)
    assert extra["refcount"] == 0
    assert extra["released_frame"] == 3
    assert extra["deadline"] == 5
    assert registry["pending_free"] == {handle: 5}

    assert reg.tick(registry, 6)["released"] == [handle]


def test_thousand_frame_churn_leaves_no_survivors():
    """1000 create/retain/release cycles (3000 frames) must end with an empty live table."""
    registry = _registry()

    for index in range(1000):
        current = registry["frame"]
        handle = _new_handle(registry, f"churn-{index}")
        reg.retain(registry, handle, current)
        reg.release(registry, handle, current)
        reg.release(registry, handle, current)
        reg.tick(registry, current + 3)

    reg.tick(registry, 4000)
    assert registry["resources"] == {}
    assert reg.leak_report(registry) == []


def test_old_handle_is_invalid_after_slot_reuse():
    """A stale handle raises KeyError even though its slot is alive again."""
    registry = _registry()
    stale = _new_handle(registry, "stale-tex")
    reg.release(registry, stale, 1)
    reg.tick(registry, 5)

    fresh = _new_handle(registry, "fresh-tex")
    assert reg.reuse_conflict(registry, stale) is True
    with pytest.raises(KeyError):
        reg.retain(registry, stale, 6)
    with pytest.raises(KeyError):
        reg.release(registry, stale, 6)

    assert reg.retain(registry, fresh, 6)["refcount"] == 2
