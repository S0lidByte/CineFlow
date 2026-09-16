"""Transactional outbox dispatcher and bounded concurrent worker execution loop."""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

from program.contracts.errors import (
    ProviderRateLimitError,
    normalize_provider_error,
)
from program.contracts.events import OutboxLifecycleEvent, OutboxLifecycleListener
from program.contracts.operation_ledger import (
    OperationLedger,
    claim_due_operations,
    complete_operation,
    fail_operation,
    renew_lease,
)
from program.contracts.operation_timeline import serialize_operation_timeline_item
from program.contracts.outbox_metrics import (
    record_claim,
    record_claim_latency,
    record_lease_renewal,
    record_listener_failure,
    record_stale_rejection,
    set_active_leases,
    set_listener_queue_depth,
)
from program.contracts.telemetry import (
    redact_text,
    reset_correlation_id,
    set_correlation_id,
)
from program.db.db import db_session
from program.managers.sse_manager import sse_manager

if TYPE_CHECKING:
    from program.program import Program

_active_dispatcher: OutboxDispatcher | None = None
_global_lifecycle_listeners: list[OutboxLifecycleListener] = []
_global_lifecycle_listeners_lock = threading.Lock()
_global_listener_executor: ThreadPoolExecutor | None = None
_global_listener_executor_lock = threading.Lock()


def _get_listener_executor() -> ThreadPoolExecutor:
    """Return a bounded background thread pool for asynchronous listener notifications."""
    global _global_listener_executor
    with _global_listener_executor_lock:
        if _global_listener_executor is None:
            _global_listener_executor = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="outbox-listener",
            )
        return _global_listener_executor


def register_outbox_lifecycle_listener(listener: OutboxLifecycleListener) -> None:
    """Register a global isolated observer for committed durable lifecycle transitions."""
    with _global_lifecycle_listeners_lock:
        _global_lifecycle_listeners.append(listener)


def publish_outbox_lifecycle_event(event_type: str, operation: dict[str, Any]) -> None:
    """Publish a committed lifecycle transition to all registered observers and SSE stream."""
    event = OutboxLifecycleEvent(event_type=event_type, operation=operation)
    with _global_lifecycle_listeners_lock:
        listeners = tuple(_global_lifecycle_listeners)

    active = _active_dispatcher
    if active is not None:
        instance_listeners = active.get_lifecycle_listeners()
        listeners = listeners + instance_listeners

    if listeners:
        executor = _get_listener_executor()
        for listener in listeners:
            executor.submit(_invoke_listener_safely, listener, event, operation.get("id"))
        try:
            work_queue = getattr(executor, "_work_queue", None)
            if work_queue is not None and hasattr(work_queue, "qsize"):
                set_listener_queue_depth(work_queue.qsize())
        except Exception:
            pass

    try:
        sse_manager.publish_event(
            "operation_timeline",
            json.dumps({**operation, "event_type": event_type}),
        )
    except Exception as sse_err:
        logger.debug(f"Failed to publish timeline SSE event: {sse_err}")


def _invoke_listener_safely(
    listener: OutboxLifecycleListener,
    event: OutboxLifecycleEvent,
    op_id: Any,
) -> None:
    try:
        listener(event)
    except Exception as listener_err:
        record_listener_failure(event.event_type)
        logger.warning(f"Outbox lifecycle listener failed for {op_id}: {listener_err}")


def notify_outbox_dispatcher() -> None:
    """Trigger an immediate wake-up tick on the active OutboxDispatcher instance if running."""
    if _active_dispatcher is not None:
        _active_dispatcher.notify()


@dataclass(frozen=True)
class _LeaseHeartbeat:
    """Cooperatively stopped heartbeat thread for one fenced operation lease."""

    operation_id: str
    claim_token: str
    stop_event: threading.Event
    thread: threading.Thread


class OutboxDispatcher:
    """
    Background worker loop for claiming and dispatching durable outbox operations.

    Features:
    - ACID-compliant dual-dialect claiming (PostgreSQL row-level locks / SQLite optimistic leases).
    - Bounded concurrent worker execution via ThreadPoolExecutor.
    - Rate-limit aware exponential backoff with jitter.
    - Dead-letter state progression for non-transient failures or exhausted retries.
    - SSE timeline event broadcasting for real-time UI streaming.
    """

    def __init__(
        self,
        program: Program | None = None,
        *,
        worker_id: str | None = None,
        max_workers: int = 5,
        poll_interval_seconds: float = 5.0,
        lease_duration_seconds: int = 300,
        max_retries: int = 5,
        base_backoff_seconds: float = 10.0,
        db_session_cm: Any = None,
    ) -> None:
        self.program = program
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.max_workers = max_workers
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_duration_seconds = lease_duration_seconds
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self._db_session_cm = db_session_cm or db_session

        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._handlers: dict[str, Callable[[OperationLedger, dict[str, Any]], Any]] = {}
        self._lifecycle_listeners: list[OutboxLifecycleListener] = []
        self._lifecycle_listeners_lock = threading.Lock()
        self._active_leases: set[str] = set()
        self._lease_heartbeats: dict[tuple[str, str], _LeaseHeartbeat] = {}
        self._leases_lock = threading.Lock()

    @property
    def _heartbeat_interval_seconds(self) -> float:
        """Return the governed lease-renewal interval for active operations."""
        return self.lease_duration_seconds / 3.0

    def register_handler(
        self,
        operation_type: str,
        handler: Callable[[OperationLedger, dict[str, Any]], Any],
    ) -> None:
        """Register a handler callback for a specific operation_type."""
        self._handlers[operation_type] = handler

    def notify(self) -> None:
        """Signal the dispatcher to wake up immediately and poll for pending operations."""
        self._wakeup_event.set()

    def register_lifecycle_listener(self, listener: OutboxLifecycleListener) -> None:
        """Register an isolated observer for committed durable lifecycle transitions."""
        with self._lifecycle_listeners_lock:
            self._lifecycle_listeners.append(listener)

    def get_lifecycle_listeners(self) -> tuple[OutboxLifecycleListener, ...]:
        """Return a snapshot of registered instance lifecycle listeners."""
        with self._lifecycle_listeners_lock:
            return tuple(self._lifecycle_listeners)

    def start(self) -> None:
        """Start the outbox dispatcher worker thread and executor."""
        global _active_dispatcher
        if self._thread is not None and self._thread.is_alive():
            return

        _active_dispatcher = self
        self._stop_event.clear()
        self._wakeup_event.clear()
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=f"outbox-{self.worker_id}",
        )
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"OutboxDispatcher-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"OutboxDispatcher [{self.worker_id}] started with max_workers={self.max_workers}")

    def stop(self, wait: bool = True) -> None:
        """Gracefully stop the outbox dispatcher and wait for in-flight tasks to complete."""
        global _active_dispatcher
        if _active_dispatcher is self:
            _active_dispatcher = None

        self._stop_event.set()
        self._wakeup_event.set()
        self._stop_all_heartbeats(wait=wait)

        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=False)
            self._executor = None

        if self._thread is not None:
            if wait:
                self._thread.join(timeout=10.0)
            self._thread = None

        logger.info(f"OutboxDispatcher [{self.worker_id}] stopped")

    def _run_loop(self) -> None:
        """Main dispatcher loop polling for due operations and renewing active leases."""
        while not self._stop_event.is_set():
            try:
                self._dispatch_due_operations()
            except Exception as e:
                logger.error(f"Error in OutboxDispatcher loop [{self.worker_id}]: {e}")

            # Wait for wake-up notification or timeout
            self._wakeup_event.wait(timeout=self.poll_interval_seconds)
            self._wakeup_event.clear()

    def _dispatch_due_operations(self) -> None:
        """Claim due operations from the database and submit them to the worker pool."""
        if self._executor is None or self._stop_event.is_set():
            return

        with self._leases_lock:
            available_slots = max(0, self.max_workers - len(self._active_leases))

        if available_slots <= 0:
            logger.debug(
                f"OutboxDispatcher [{self.worker_id}] at full capacity ({len(self._active_leases)}/{self.max_workers} active leases). Skipping claim."
            )
            return

        claimed_ops: list[tuple[dict[str, Any], str]] = []
        t0 = time.perf_counter()

        try:
            with self._db_session_cm() as session:
                ops = claim_due_operations(
                    session=session,
                    worker_id=self.worker_id,
                    limit=available_slots,
                    lease_duration_seconds=self.lease_duration_seconds,
                )
                if ops:
                    claimed_ops = [
                        (serialize_operation_timeline_item(op), op.claim_token or "")
                        for op in ops
                    ]
                    session.commit()
        except Exception as claim_err:
            logger.error(f"Failed to claim due outbox operations: {claim_err}")
            return
        finally:
            record_claim_latency(time.perf_counter() - t0)

        for operation, claim_token in claimed_ops:
            op_id = operation["id"]
            op_type = operation["operation_type"]
            payload = operation["payload"]
            attempt_count = operation["attempt_count"]
            correlation_id = operation["correlation_id"]
            media_item_id = operation["media_item_id"]
            if not claim_token:
                logger.warning(f"Outbox operation {op_id} has no claim token")
                continue
            if self._stop_event.is_set():
                break

            record_claim(op_type)
            self._publish_lifecycle_event("operation_started", operation)

            with self._leases_lock:
                self._active_leases.add(op_id)
                set_active_leases(len(self._active_leases))
            self._start_lease_heartbeat(op_id, claim_token)

            self._executor.submit(
                self._execute_operation,
                op_id,
                op_type,
                payload,
                attempt_count,
                correlation_id,
                media_item_id,
                claim_token,
            )

    def _execute_operation(
        self,
        op_id: str,
        op_type: str,
        payload: dict[str, Any] | None,
        attempt_count: int,
        correlation_id: str,
        media_item_id: int | None,
        claim_token: str,
    ) -> None:
        """Execute a single operation inside correlation context with error handling and backoff."""
        token = set_correlation_id(correlation_id)
        payload = payload or {}

        try:
            handler = self._handlers.get(op_type)
            if handler is not None:
                handler_arg = OperationLedger(
                    id=op_id,
                    correlation_id=correlation_id,
                    media_item_id=media_item_id,
                    operation_type=op_type,
                    payload=payload,
                    attempt_count=attempt_count,
                    claim_token=claim_token,
                    worker_id=self.worker_id,
                )
                handler(handler_arg, payload)
            else:
                # Default handler: pass-through logging
                logger.debug(
                    f"Outbox operation {op_id} ({op_type}) executed (no custom handler registered)"
                )

            # Persist before notifying observers or SSE consumers.
            completed_operation: dict[str, Any] | None = None
            with self._db_session_cm() as session:
                completed = complete_operation(
                    session,
                    op_id,
                    worker_id=self.worker_id,
                    claim_token=claim_token,
                )
                if completed is not None:
                    completed_operation = serialize_operation_timeline_item(completed)
                    session.commit()
                else:
                    session.rollback()

            if completed_operation is None:
                record_stale_rejection("complete")
                logger.warning(f"Outbox operation {op_id} lost ownership before completion")
                return

            self._publish_lifecycle_event("operation_completed", completed_operation)
            logger.info(f"Outbox operation {op_id} ({op_type}) successfully completed")

        except Exception as exc:
            self._handle_operation_failure(
                op_id=op_id,
                op_type=op_type,
                exc=exc,
                attempt_count=attempt_count,
                correlation_id=correlation_id,
                media_item_id=media_item_id,
                claim_token=claim_token,
            )
        finally:
            self._stop_lease_heartbeat(op_id, claim_token)
            with self._leases_lock:
                self._active_leases.discard(op_id)
                set_active_leases(len(self._active_leases))
            reset_correlation_id(token)

    def _start_lease_heartbeat(self, operation_id: str, claim_token: str) -> None:
        """Start one cooperative renewal worker after a lease has been committed."""
        stop_event = threading.Event()
        heartbeat = _LeaseHeartbeat(
            operation_id=operation_id,
            claim_token=claim_token,
            stop_event=stop_event,
            thread=threading.Thread(
                target=self._run_lease_heartbeat,
                args=(operation_id, claim_token, stop_event),
                name=f"OutboxLeaseHeartbeat-{self.worker_id}-{operation_id}",
                daemon=True,
            ),
        )
        key = (operation_id, claim_token)
        with self._leases_lock:
            existing = self._lease_heartbeats.get(key)
            if existing is not None:
                return
            self._lease_heartbeats[key] = heartbeat
        heartbeat.thread.start()

    def _run_lease_heartbeat(
        self,
        operation_id: str,
        claim_token: str,
        stop_event: threading.Event,
    ) -> None:
        """Renew one active lease until its execution or dispatcher is stopped."""
        while not self._stop_event.is_set() and not stop_event.wait(self._heartbeat_interval_seconds):
            if self._stop_event.is_set():
                break
            try:
                with self._db_session_cm() as session:
                    renewed = renew_lease(
                        session,
                        operation_id,
                        worker_id=self.worker_id,
                        claim_token=claim_token,
                        extension_seconds=self.lease_duration_seconds,
                    )
                    if renewed:
                        session.commit()
                        record_lease_renewal("success")
                    else:
                        session.rollback()
                        record_lease_renewal("rejected")
                        record_stale_rejection("heartbeat")
            except Exception as exc:
                record_lease_renewal("error")
                logger.warning(
                    f"Outbox lease heartbeat failed for {operation_id} "
                    f"[{self.worker_id}]: {exc}"
                )
                stop_event.set()
                return

            if not renewed:
                logger.warning(
                    f"Outbox lease heartbeat lost ownership for {operation_id} "
                    f"[{self.worker_id}]; stopping renewal"
                )
                stop_event.set()
                return

    def _stop_lease_heartbeat(self, operation_id: str, claim_token: str) -> None:
        """Signal and reap a heartbeat without joining the current thread."""
        key = (operation_id, claim_token)
        with self._leases_lock:
            heartbeat = self._lease_heartbeats.pop(key, None)
        if heartbeat is None:
            return
        heartbeat.stop_event.set()
        if heartbeat.thread is not threading.current_thread():
            heartbeat.thread.join(timeout=max(1.0, self._heartbeat_interval_seconds + 1.0))

    def _stop_all_heartbeats(self, *, wait: bool) -> None:
        """Stop every active heartbeat before dispatcher shutdown continues."""
        with self._leases_lock:
            heartbeats = list(self._lease_heartbeats.values())
            self._lease_heartbeats.clear()
        for heartbeat in heartbeats:
            heartbeat.stop_event.set()
        if wait:
            for heartbeat in heartbeats:
                if heartbeat.thread is not threading.current_thread():
                    heartbeat.thread.join(timeout=max(1.0, self._heartbeat_interval_seconds + 1.0))

    def _handle_operation_failure(
        self,
        op_id: str,
        op_type: str,
        exc: Exception,
        attempt_count: int,
        correlation_id: str,
        media_item_id: int | None,
        claim_token: str,
    ) -> None:
        """Classify failure, compute jittered backoff or dead-letter, and persist state."""
        normalized = normalize_provider_error(exc, provider_name=op_type)
        err_class = type(normalized).__name__
        err_msg = redact_text(str(normalized))

        is_transient = normalized.is_transient
        should_retry = is_transient and (attempt_count < self.max_retries)

        next_retry_at: datetime | None = None
        if should_retry:
            if isinstance(normalized, ProviderRateLimitError) and normalized.retry_after_seconds:
                delay = float(normalized.retry_after_seconds)
            else:
                backoff = self.base_backoff_seconds * (2 ** max(0, attempt_count - 1))
                jitter = random.uniform(0.1, 1.5)
                delay = backoff + jitter

            next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
            logger.warning(
                f"Outbox operation {op_id} ({op_type}) transient failure [{err_class}]: "
                f"Retry {attempt_count}/{self.max_retries} scheduled in {delay:.1f}s"
            )
        else:
            logger.error(
                f"Outbox operation {op_id} ({op_type}) failed permanently [{err_class}]: {err_msg}"
            )

        failed_operation: dict[str, Any] | None = None
        event_type = "operation_retrying" if should_retry else "operation_failed"
        with self._db_session_cm() as session:
            failed = fail_operation(
                session,
                op_id,
                worker_id=self.worker_id,
                claim_token=claim_token,
                error_classification=err_class,
                error_message=err_msg,
                next_retry_at=next_retry_at,
            )
            if failed is not None:
                failed_operation = serialize_operation_timeline_item(failed)
                session.commit()
            else:
                session.rollback()

        if failed_operation is None:
            record_stale_rejection("fail")
            logger.warning(f"Outbox operation {op_id} lost ownership before failure persistence")
            return

        self._publish_lifecycle_event(event_type, failed_operation)

    def _publish_lifecycle_event(self, event_type: str, operation: dict[str, Any]) -> None:
        """Notify isolated listeners and publish the canonical record after commit."""
        publish_outbox_lifecycle_event(event_type, operation)
