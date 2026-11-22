"""Regression tests for D81: proactive READY_TRANSIENT entries must not leak.

Root cause (D77 regression): when _cache_chunk() received SKIPPED_PREFETCH_PRESSURE
or REFUSED_PHYSICAL_PRESSURE from cache.put(), it called
_delivery_registry.publish(start, payload) even when NO reader had registered
via register_pending().  The original publish() created a _DeliveryEntry with
waiter_count=1 and no matching release_reservation() / release_borrow() — the
entry stayed forever in _entries[start].

Downstream breakage:
  • has_pending(start) returned True → misclassified the prefetch chunk as a
    "demand" fetch → cache-pressure path applied wrong admission tier.
  • _run_opportunistic_prefetch re-evaluated the same chunk on every iteration
    (get_prefetch_uncached returned it because it was still uncached), hit the
    stale transient entry and broke out of the loop → streaming froze every ~1 s.

Fix (D81):
  • publish() with no existing entry now creates a zero-waiter (waiter_count=0)
    READY_TRANSIENT entry with a TTL timestamp.
  • has_pending() now only returns True for PENDING entries or READY_TRANSIENT
    entries that have at least one registered waiter (waiter_count > 0).
  • _DeliveryRegistry.expire_stale_transients() removes zero-waiter
    READY_TRANSIENT entries that exceeded _TRANSIENT_TTL_SECONDS.
  • _run_opportunistic_prefetch() calls expire_stale_transients() at the top of
    each iteration to prevent stale proactive entries from corrupting the loop.
"""

from __future__ import annotations

from time import monotonic

import trio

from program.services.streaming.media_stream import (
    _DeliveryEntry,
    _DeliveryRegistry,
    _DeliveryState,
)

# ---------------------------------------------------------------------------
# has_pending() semantics
# ---------------------------------------------------------------------------


def test_has_pending_false_when_registry_empty() -> None:
    """has_pending returns False when no entry exists."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        assert not registry.has_pending(0)

    trio.run(exercise)


def test_has_pending_true_for_pending_entry_with_waiters() -> None:
    """has_pending returns True for a PENDING entry with at least one waiter."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        await registry.register_pending(0)
        assert registry.has_pending(0)

    trio.run(exercise)


def test_has_pending_false_for_proactive_ready_transient() -> None:
    """Core regression: has_pending must return False for a proactively-published
    zero-waiter READY_TRANSIENT entry.

    This is the exact scenario that caused per-second streaming freezes in D77:
    prefetch published bytes for a cache-refused chunk with no waiting reader,
    creating a waiter_count=1 stale entry.  has_pending() returned True, which
    misclassified the next prefetch attempt as a demand fetch.
    """

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        # Simulate what _cache_chunk() does on SKIPPED_PREFETCH_PRESSURE:
        # publish bytes without any prior register_pending().
        await registry.publish(start=0, payload=b"chunk_data")

        # Entry exists and is READY_TRANSIENT ...
        entry = registry.get_entry(0)
        assert entry is not None
        assert entry.state == _DeliveryState.READY_TRANSIENT

        # ... but has_pending() must NOT report it as an in-flight demand fetch.
        assert not registry.has_pending(0), (
            "has_pending() must return False for a proactive zero-waiter "
            "READY_TRANSIENT entry — returning True triggers D77 regression "
            "(prefetch freeze every ~1 s)"
        )

    trio.run(exercise)


def test_has_pending_true_for_ready_transient_with_registered_waiter() -> None:
    """has_pending returns True when a READY_TRANSIENT entry has an active waiter
    (reader called register_pending before the payload arrived)."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        # Reader registers before the payload arrives.
        await registry.register_pending(0)
        # Producer publishes.
        await registry.publish(start=0, payload=b"chunk_data")

        entry = registry.get_entry(0)
        assert entry is not None
        assert entry.state == _DeliveryState.READY_TRANSIENT
        assert entry.waiter_count >= 1

        # This entry has a real waiter — has_pending should return True so the
        # connection loop knows a foreground read is blocked on this chunk.
        assert registry.has_pending(0)

    trio.run(exercise)


# ---------------------------------------------------------------------------
# proactive publish: zero waiter_count, no leak
# ---------------------------------------------------------------------------


def test_proactive_publish_sets_waiter_count_zero() -> None:
    """When publish() is called with no prior register_pending(), the resulting
    READY_TRANSIENT entry must have waiter_count == 0.

    waiter_count=1 (the D77 bug) caused the entry to never be cleaned up by
    release_reservation() or release_borrow() since no reader owned it.
    """

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        await registry.publish(start=0, payload=b"bytes")
        entry = registry.get_entry(0)
        assert entry is not None
        assert entry.waiter_count == 0, (
            "Proactively-published entry must NOT have waiter_count=1 — "
            "that caused the D77 stale-entry / prefetch-freeze regression"
        )

    trio.run(exercise)


def test_proactive_publish_payload_readable_via_get_entry() -> None:
    """A proactively-published entry must still serve its payload to late readers
    that arrive after the publish via get_entry() / _read_cached_or_fallback()."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        payload = b"chunk_payload_12345"
        await registry.publish(start=0, payload=payload)

        entry = registry.get_entry(0)
        assert entry is not None
        assert entry.state == _DeliveryState.READY_TRANSIENT
        assert entry.payload == payload
        assert entry.ready.is_set()

    trio.run(exercise)


def test_register_pending_on_proactive_entry_increments_waiter_count() -> None:
    """When a reader calls register_pending() after a proactive publish, the
    existing READY_TRANSIENT entry is reused and waiter_count is incremented."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        await registry.publish(start=0, payload=b"data")

        # Reader arrives after the publish.
        entry = await registry.register_pending(0)
        assert entry.state == _DeliveryState.READY_TRANSIENT
        assert entry.waiter_count == 1
        # entry.ready is already set so the reader can proceed immediately.
        assert entry.ready.is_set()

    trio.run(exercise)


def test_register_pending_cleanup_after_proactive_publish() -> None:
    """When a reader registers, reads, and releases a proactively-published entry,
    the entry is fully cleaned up from _entries."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        await registry.publish(start=0, payload=b"data")
        entry = await registry.register_pending(0)
        assert entry.waiter_count == 1

        await registry.release_reservation(entry)
        # Entry should now be gone (waiter_count returned to 0 → retired + evicted).
        assert registry.get_entry(0) is None

    trio.run(exercise)


# ---------------------------------------------------------------------------
# TTL expiry via expire_stale_transients()
# ---------------------------------------------------------------------------


def test_expire_stale_transients_removes_old_zero_waiter_entries() -> None:
    """expire_stale_transients() must evict READY_TRANSIENT entries whose
    registered_at timestamp is older than _TRANSIENT_TTL_SECONDS and that have
    no active waiters or borrowers."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        await registry.publish(start=0, payload=b"stale_chunk")

        entry = registry.get_entry(0)
        assert entry is not None

        # Backdate the entry to simulate it being older than the TTL.
        old_time = monotonic() - registry._TRANSIENT_TTL_SECONDS - 1.0
        entry.registered_at = old_time

        await registry.expire_stale_transients()

        # The stale entry should have been removed.
        assert registry.get_entry(0) is None

    trio.run(exercise)


def test_expire_stale_transients_keeps_recent_zero_waiter_entries() -> None:
    """expire_stale_transients() must NOT evict entries that are still within the
    TTL window (even if they have no waiters)."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        await registry.publish(start=0, payload=b"fresh_chunk")

        await registry.expire_stale_transients()

        # Entry is fresh — must NOT be evicted.
        entry = registry.get_entry(0)
        assert entry is not None
        assert entry.state == _DeliveryState.READY_TRANSIENT

    trio.run(exercise)


def test_expire_stale_transients_keeps_entries_with_active_waiters() -> None:
    """expire_stale_transients() must NOT evict entries that have active waiters,
    even if they are past the TTL."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        # Reader registers, then payload arrives.
        await registry.register_pending(0)
        await registry.publish(start=0, payload=b"active_chunk")
        entry = registry.get_entry(0)
        assert entry is not None
        assert entry.waiter_count >= 1

        # Backdate to trigger TTL.
        entry.registered_at = monotonic() - registry._TRANSIENT_TTL_SECONDS - 1.0

        await registry.expire_stale_transients()

        # Must NOT be evicted — there's an active waiter.
        assert registry.get_entry(0) is not None

    trio.run(exercise)


def test_expire_stale_transients_retires_and_unblocks_late_waiters() -> None:
    """When a stale proactive entry is swept, it must be transitioned to RETIRED
    and have ready.set() called so that any task already waiting on entry.ready
    is unblocked."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        await registry.publish(start=0, payload=b"data")

        entry = registry.get_entry(0)
        assert entry is not None
        # Backdate.
        entry.registered_at = monotonic() - registry._TRANSIENT_TTL_SECONDS - 1.0

        # Simulate a task that grabbed the entry object before expiry.
        # entry.ready is already set (it was published), so this doesn't block.
        await entry.ready.wait()

        await registry.expire_stale_transients()

        assert entry.state == _DeliveryState.RETIRED
        assert entry.ready.is_set()

    trio.run(exercise)


def test_expire_stale_transients_no_effect_on_pending_entries() -> None:
    """expire_stale_transients() must leave PENDING entries untouched regardless
    of age — PENDING entries have active waiters and must not be prematurely retired."""

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        entry = await registry.register_pending(0)
        # Backdate to simulate old entry.
        entry.registered_at = monotonic() - registry._TRANSIENT_TTL_SECONDS - 1.0

        await registry.expire_stale_transients()

        assert registry.get_entry(0) is entry
        assert entry.state == _DeliveryState.PENDING

    trio.run(exercise)


# ---------------------------------------------------------------------------
# Full round-trip: proactive publish followed by reader
# ---------------------------------------------------------------------------


def test_proactive_publish_then_demand_reader_roundtrip() -> None:
    """End-to-end scenario mirroring the D77 regression:
    1. Prefetch fetches chunk → cache refuses → publish() called with no waiting reader.
    2. VFS read arrives for the same chunk.
    3. read_bytes path calls register_pending → finds READY_TRANSIENT entry.
    4. Reader reads payload, releases reservation.
    5. Entry is cleaned up.
    """

    async def exercise() -> None:
        registry = _DeliveryRegistry()
        payload = b"media_chunk_data_x" * 10

        # Step 1: Prefetch publishes bytes proactively (cache refused admission).
        await registry.publish(start=0, payload=payload)
        assert not registry.has_pending(0)  # D81 invariant: no active waiters

        # Step 2+3: VFS read arrives, calls register_pending.
        entry = await registry.register_pending(0)
        assert entry.state == _DeliveryState.READY_TRANSIENT
        assert registry.has_pending(0)  # Now there's a real waiter

        # Step 4: Reader reads payload.
        assert entry.payload == payload

        # Step 5: Reader releases.
        await registry.release_reservation(entry)
        assert registry.get_entry(0) is None  # Fully cleaned up

    trio.run(exercise)
