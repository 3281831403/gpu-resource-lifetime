"""Deferred-release GPU resource lifetime registry.

The registry tracks GPU resources (textures, buffers, pipelines, ...) whose
handles may outlive the frame that dropped the last reference.  Handles are
never freed immediately: a resource whose reference count reaches zero is
parked in a delayed-free queue and is reclaimed some frames later, so code
that released early in a frame can still use the resource for the rest of
that frame.

Semantics (the whole exercise hinges on these three numbers being right)
-----------------------------------------------------------------------
* Handle uniqueness: a handle is a non-negative ``int`` laid out as::

      handle = (generation << SLOT_BITS) | slot

  ``slot`` identifies the storage bucket and is recycled after the resource
  is reclaimed.  ``generation`` starts at 1 for a slot and is incremented by
  exactly 1 every time that slot is handed out again.  Therefore every
  handle ever issued is unique; an old handle whose slot has been recycled
  is *stale* and all mutating calls on it raise ``KeyError``.
* Release delay: ``release`` at frame ``f`` parks the resource with
  ``deadline = f + RELEASE_DELAY_FRAMES`` (``RELEASE_DELAY_FRAMES == 2``).
  The resource stays in the live table and remains fully usable while
  ``frame <= deadline`` (frames ``f``, ``f + 1`` and ``f + 2``).  ``tick``
  reclaims it only when called with ``frame > deadline`` (i.e. earliest at
  ``f + 3``).  The boundary is strict; the tolerance is zero -- there is no
  "about two frames" behaviour.
* Leak classification: an entry of the live table is leaked when it still
  has references but has not been used for more than
  ``LEAK_AFTER_FRAMES`` frames, or when its reference count is zero while it
  is not parked in the delayed-free queue (a stuck/orphaned entry).  A
  resource that is merely waiting in the delayed-free queue is *not* a leak.

Registry layout (a plain dict; the caller starts it as ``{}`` and the
implementation must initialise any missing key on first use)::

    {
        "frame": int,                  # frame of the last tick(), starts at 0
        "next_slot": int,              # next never-used slot, starts at 0
        "free_slots": list[int],       # reclaimed slots, reused LIFO
        "generations": dict[int, int], # slot -> current generation (>= 1)
        "resources": dict[int, dict],  # slot -> live resource record
        "pending_free": dict[int, int],# handle -> reclaim deadline frame
    }

A live resource record is::

    {
        "handle": int,
        "desc": str,
        "refcount": int,
        "created_frame": int,
        "last_used_frame": int,
        "edges": list[int],            # outgoing references to other handles
        "released_frame": int | None,  # frame whose release hit zero
        "deadline": int | None,        # reclaim deadline while parked
    }

``create``, ``retain`` and ``release`` return a shallow snapshot (a plain
``dict`` copy) of the affected record; the authoritative state lives in
``registry["resources"]``.  ``tick`` returns
``{"frame": frame, "released": [handle, ...]}`` where ``released`` lists the
handles reclaimed by that call in ascending order.

The module uses only the Python standard library, is fully deterministic and
never reads a clock: every frame number is passed in explicitly by the
caller.
"""

from __future__ import annotations

#: Number of frames a zero-reference resource survives before it is reclaimed.
#: A release at frame ``f`` sets ``deadline = f + RELEASE_DELAY_FRAMES`` and
#: the resource stays alive through ``f + RELEASE_DELAY_FRAMES`` inclusive.
RELEASE_DELAY_FRAMES = 2

#: A resource with references that has not been touched for more frames than
#: this is reported as a leak.
LEAK_AFTER_FRAMES = 600

#: Number of low bits of a handle used for the slot index.
SLOT_BITS = 32

#: Bit mask extracting the slot index from a handle.
SLOT_MASK = (1 << SLOT_BITS) - 1

__all__ = [
    "LEAK_AFTER_FRAMES",
    "RELEASE_DELAY_FRAMES",
    "SLOT_BITS",
    "SLOT_MASK",
    "create",
    "cycle_detect",
    "leak_report",
    "release",
    "retain",
    "reuse_conflict",
    "tick",
    "validate_registry",
]


def _init(registry: dict) -> None:
    """Initialise any missing registry key on first use."""
    registry.setdefault("frame", 0)
    registry.setdefault("next_slot", 0)
    registry.setdefault("free_slots", [])
    registry.setdefault("generations", {})
    registry.setdefault("resources", {})
    registry.setdefault("pending_free", {})


def _lookup(registry: dict, handle: int) -> dict:
    """Return the live record for ``handle`` or raise ``KeyError``.

    The slot must exist in the live table and the stored handle must match
    exactly; a handle whose slot was recycled at a newer generation is stale.
    """
    slot = handle & SLOT_MASK
    record = registry["resources"].get(slot)
    if record is None or record["handle"] != handle:
        raise KeyError(handle)
    return record


def create(registry: dict, desc: str) -> dict:
    """Allocate a new resource described by ``desc`` and return its record.

    Contract
    --------
    * Pick a slot: pop ``registry["free_slots"]`` (LIFO) when non-empty,
      otherwise take ``registry["next_slot"]`` and increment it.  Increment
      ``registry["generations"][slot]`` (starting at 1 for a fresh slot) and
      build ``handle = (generation << SLOT_BITS) | slot``.  The handle is
      unique for the process lifetime; the same slot may be reused only with
      a strictly larger generation.
    * Insert a live record into ``registry["resources"][slot]`` with
      ``refcount == 1``, ``created_frame == last_used_frame ==
      registry["frame"]``, ``edges == []``, ``released_frame is None`` and
      ``deadline is None``.
    * Return a snapshot copy of the record.  No tolerance: the generation is
      bumped by exactly one per allocation of that slot, and the reference
      count starts at exactly one.
    """
    _init(registry)
    if registry["free_slots"]:
        slot = registry["free_slots"].pop()
    else:
        slot = registry["next_slot"]
        registry["next_slot"] = slot + 1
    generation = registry["generations"].get(slot, 0) + 1
    registry["generations"][slot] = generation
    handle = (generation << SLOT_BITS) | slot
    record = {
        "handle": handle,
        "desc": desc,
        "refcount": 1,
        "created_frame": registry["frame"],
        "last_used_frame": registry["frame"],
        "edges": [],
        "released_frame": None,
        "deadline": None,
    }
    registry["resources"][slot] = record
    return dict(record)


def retain(registry: dict, handle: int, frame: int) -> dict:
    """Add one reference to ``handle`` and record ``frame`` as its last use.

    Contract
    --------
    * Validate the handle against the live table: the slot must exist and
      ``resources[slot]["handle"]`` must equal ``handle``.  A stale handle
      from a recycled slot (or an unknown handle) raises ``KeyError``.
    * Increment ``refcount`` by one and set ``last_used_frame = frame``.
      Refreshing ``last_used_frame`` is mandatory: ``leak_report`` depends on
      it, and a resource retained in the current frame must not be reported
      as leaked.
    * If the resource was parked in ``pending_free`` (refcount had reached
      zero but the reclaim deadline has not passed), remove it from the queue
      and clear ``released_frame``/``deadline``: the resource is alive again
      and the pending release is cancelled.  This makes a zero-reference
      resource usable again inside the same frame.
    * ``frame`` must be at least ``registry["frame"]`` (the caller passes the
      current frame); it is never inferred from a clock.  Return a snapshot
      of the updated record.
    """
    _init(registry)
    record = _lookup(registry, handle)
    record["refcount"] += 1
    record["last_used_frame"] = frame
    if handle in registry["pending_free"]:
        del registry["pending_free"][handle]
        record["released_frame"] = None
        record["deadline"] = None
    return dict(record)


def release(registry: dict, handle: int, frame: int) -> dict:
    """Drop one reference to ``handle``; park it when the count hits zero.

    Contract
    --------
    * Validate the handle exactly like ``retain``; a stale or unknown handle
      raises ``KeyError``.
    * Decrement ``refcount`` by exactly one.  The count must never become
      negative: a release on a resource whose ``refcount`` is already zero is
      suppressed and leaves the record untouched (same ``refcount``,
      ``released_frame`` and ``deadline``); it is not an error.
    * When the decrement reaches zero, do **not** free the resource now.
      Set ``released_frame = frame``, set
      ``deadline = frame + RELEASE_DELAY_FRAMES`` and register the handle in
      ``registry["pending_free"]`` with that deadline.  The resource remains
      in the live table and stays usable through the deadline frame; only
      ``tick`` may reclaim it.
    * When the decrement leaves the count positive, the resource is not
      queued.  Return a snapshot of the resulting record.  Tolerance is zero:
      no off-by-one frames, no early free.
    * ``frame`` must be at least ``registry["frame"]`` (the caller passes the
      current frame); it is never inferred from a clock.
    """
    _init(registry)
    record = _lookup(registry, handle)
    if record["refcount"] == 0:
        # Double release is suppressed: the count never goes negative and
        # the parked record keeps its original release frame and deadline.
        return dict(record)
    record["refcount"] -= 1
    if record["refcount"] == 0:
        record["released_frame"] = frame
        record["deadline"] = frame + RELEASE_DELAY_FRAMES
        registry["pending_free"][handle] = record["deadline"]
    return dict(record)


def tick(registry: dict, frame: int) -> dict:
    """Advance the registry clock to ``frame`` and reclaim expired resources.

    Contract
    --------
    * ``frame`` is an explicit integer and must not go backwards:
      ``frame >= registry["frame"]``; otherwise raise ``ValueError``.  Set
      ``registry["frame"] = frame``.
    * Reclaim **every** parked resource with ``deadline < frame`` in one
      pass; the boundary comparison is strict, so a resource with
      ``deadline == frame`` is still alive after this call (no early
      reclaim by one frame).
    * Reclaiming means: delete the record from ``registry["resources"]``,
      delete the handle from ``registry["pending_free"]``, and return the
      slot to ``registry["free_slots"]`` for reuse.  The slot keeps its
      generation; ``create`` bumps it on the next allocation, which is what
      makes the old handle detectable as stale.
    * Return ``{"frame": frame, "released": [handle, ...]}`` with the
      reclaimed handles in ascending order.  A tick with nothing expired
      returns ``"released": []``.  Calling ``tick`` again with the same frame
      is a no-op.  Tolerance is zero: one tick releases all expired entries,
      and only those.
    """
    _init(registry)
    if frame < registry["frame"]:
        raise ValueError(f"frame went backwards: {frame} < {registry['frame']}")
    registry["frame"] = frame
    expired = [
        handle
        for handle, deadline in registry["pending_free"].items()
        if deadline < frame
    ]
    released = []
    for handle in expired:
        slot = handle & SLOT_MASK
        record = registry["resources"].get(slot)
        if record is not None and record["handle"] == handle:
            del registry["resources"][slot]
            registry["free_slots"].append(slot)
        del registry["pending_free"][handle]
        released.append(handle)
    released.sort()
    return {"frame": frame, "released": released}


def reuse_conflict(registry: dict, handle: int) -> bool:
    """Return whether ``handle`` was invalidated by slot reuse.

    Contract
    --------
    * Decode ``slot = handle & SLOT_MASK`` and
      ``generation = handle >> SLOT_BITS``.
    * Return ``True`` exactly when the slot has been allocated again at a
      higher generation, i.e. ``registry["generations"].get(slot, 0) >
      generation``.  That is the handle-reuse conflict: the same slot number
      now belongs to a different, newer resource.
    * Return ``False`` for a currently valid handle and for a handle whose
      slot was never issued.  This predicate looks only at generations; it
      must not be fooled into comparing just the slot number, because every
      recycled slot keeps its id.
    """
    _init(registry)
    slot = handle & SLOT_MASK
    generation = handle >> SLOT_BITS
    return registry["generations"].get(slot, 0) > generation


def leak_report(registry: dict) -> list[str]:
    """Return one human-readable string per leaked resource (empty if none).

    Contract
    --------
    * The current frame is ``registry["frame"]`` (maintained by ``tick``);
      frame numbers are never read from a clock.
    * A live resource is leaked when either

      - ``refcount > 0`` and ``registry["frame"] - last_used_frame >
        LEAK_AFTER_FRAMES`` (alive but untouched for too long), or
      - ``refcount == 0`` and the handle is **not** in
        ``registry["pending_free"]`` (stuck zero-reference entry).

    * A resource parked in the delayed-free queue has ``refcount == 0`` but
      is *not* a leak; it must not be reported, and no resource may appear
      twice in the result.  Refreshing ``last_used_frame`` in ``retain`` is
      what keeps active resources out of this report.
    * Sort the result by handle (ascending).  Each entry must contain the
      resource's ``desc`` and its decimal ``handle`` so a caller can act on
      it.  Return ``[]`` for a clean registry.
    """
    _init(registry)
    frame = registry["frame"]
    pending = registry["pending_free"]
    leaked = []
    for slot, record in registry["resources"].items():
        handle = record["handle"]
        if record["refcount"] > 0:
            if frame - record["last_used_frame"] > LEAK_AFTER_FRAMES:
                leaked.append(record)
        elif handle not in pending:
            leaked.append(record)
    leaked.sort(key=lambda record: record["handle"])
    return [
        f"leaked resource {record['desc']!r} (handle {record['handle']})"
        for record in leaked
    ]


def cycle_detect(registry: dict) -> list[list[str]]:
    """Return every reference cycle among live resources, as description lists.

    Contract
    --------
    * Nodes are the live resources; a directed edge goes from a resource to
      each handle in its ``edges`` list that still resolves to a live
      resource (edges to reclaimed/unknown handles are ignored).
    * Find **all** simple cycles, including multi-node cycles (A -> B -> C ->
      A), not only self loops (A -> A).  A self loop is returned as a
      one-element list.
    * Each cycle is a list of descriptions in edge order, rotated so that
      the lexicographically smallest description comes first.  The list of
      cycles is sorted by its first element.  Return ``[]`` when the graph is
      acyclic.  The result is deterministic: no duplicates and no dependence
      on dict insertion order.
    """
    _init(registry)
    resources = registry["resources"]
    # Map live handles to their slot so edges to reclaimed/unknown handles
    # are ignored.
    live_slots_by_handle = {
        record["handle"]: slot for slot, record in resources.items()
    }
    adjacency: dict[int, list[int]] = {}
    for slot, record in resources.items():
        targets = []
        for edge in record["edges"]:
            target = live_slots_by_handle.get(edge)
            if target is not None and target not in targets:
                targets.append(target)
        adjacency[slot] = sorted(targets)

    nodes = sorted(resources)
    rank = {slot: index for index, slot in enumerate(nodes)}
    cycles: set[tuple[str, ...]] = set()

    # Enumerate simple cycles: start from each node and only visit nodes
    # whose rank is >= the start's, so every cycle is found exactly once
    # (from its lowest-ranked member).
    for start in nodes:
        stack: list[tuple[int, list[int]]] = [(start, [start])]
        while stack:
            node, path = stack.pop()
            for nxt in adjacency[node]:
                if nxt == start:
                    descs = [resources[s]["desc"] for s in path]
                    pivot = descs.index(min(descs))
                    rotated = tuple(descs[pivot:] + descs[:pivot])
                    cycles.add(rotated)
                elif rank[nxt] > rank[start] and nxt not in path:
                    stack.append((nxt, path + [nxt]))

    return [list(cycle) for cycle in sorted(cycles)]


def validate_registry(registry: dict) -> list[str]:
    """Audit internal consistency and return a list of problems (empty if OK).

    Contract
    --------
    * Check reference counts: every ``refcount`` is an ``int`` and ``>= 0``.
    * Check handle generations: for every slot key of
      ``registry["resources"]``, the stored ``handle`` decodes to that same
      slot and its generation equals
      ``registry["generations"][slot]`` (and is ``>= 1``).
    * Check the delayed-free queue against the live table: every handle in
      ``registry["pending_free"]`` must have a live record in
      ``registry["resources"]`` with that exact handle and ``refcount == 0``
      and ``released_frame is not None``, and its stored deadline must equal
      ``released_frame + RELEASE_DELAY_FRAMES``.  A queue entry whose live
      record has vanished is a dangling queue entry and must be reported (the
      queue must be cleaned up by ``tick``).
    * Check the other direction: a live record with ``refcount == 0`` that is
      not present in ``pending_free`` is stuck and must be reported.
    * Each problem is one string that names the offending slot/handle in
      decimal.  Return ``[]`` for a consistent registry; this function never
      mutates the registry.
    """
    _init(registry)
    problems: list[str] = []
    resources = registry["resources"]
    generations = registry["generations"]
    pending = registry["pending_free"]

    for slot, record in resources.items():
        refcount = record["refcount"]
        if not isinstance(refcount, int) or isinstance(refcount, bool) or refcount < 0:
            problems.append(f"slot {slot}: invalid refcount {refcount!r}")
        handle = record["handle"]
        if (handle & SLOT_MASK) != slot:
            problems.append(
                f"slot {slot}: handle {handle} decodes to a different slot"
            )
        generation = handle >> SLOT_BITS
        if generation < 1 or generations.get(slot) != generation:
            problems.append(
                f"slot {slot}: handle {handle} generation {generation} does not "
                f"match recorded generation {generations.get(slot)}"
            )
        if refcount == 0 and handle not in pending:
            problems.append(
                f"slot {slot}: handle {handle} has zero references but is not "
                f"in the delayed-free queue (stuck entry)"
            )

    for handle, deadline in pending.items():
        slot = handle & SLOT_MASK
        record = resources.get(slot)
        if record is None or record["handle"] != handle:
            problems.append(
                f"pending_free handle {handle}: no live record (dangling queue entry)"
            )
            continue
        if record["refcount"] != 0:
            problems.append(
                f"pending_free handle {handle}: live record has refcount "
                f"{record['refcount']} instead of 0"
            )
        if record["released_frame"] is None:
            problems.append(
                f"pending_free handle {handle}: live record has no released_frame"
            )
        elif deadline != record["released_frame"] + RELEASE_DELAY_FRAMES:
            problems.append(
                f"pending_free handle {handle}: deadline {deadline} does not equal "
                f"released_frame {record['released_frame']} + {RELEASE_DELAY_FRAMES}"
            )

    return problems
