"""Declarative state transition matrix and validation rules for CineFlow media lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from program.media.state import States


@dataclass(frozen=True, slots=True)
class StateTransitionRule:
    """Represents declarative validation rules for a state transition."""

    from_state: States
    to_state: States
    allowed_emitters: frozenset[str]
    is_transient: bool = False
    description: str = ""


# Immutable transition matrix mapping origin states to permitted target states
ALLOWED_TRANSITIONS: Final[Mapping[States, frozenset[States]]] = {
    States.Unknown: frozenset(
        {
            States.Unknown,
            States.Unreleased,
            States.Ongoing,
            States.Requested,
            States.Indexed,
            States.Scraped,
            States.Downloaded,
            States.Symlinked,
            States.Completed,
            States.PartiallyCompleted,
            States.Failed,
            States.Paused,
        }
    ),
    States.Unreleased: frozenset(
        {
            States.Unreleased,
            States.Ongoing,
            States.Requested,
            States.Indexed,
            States.Scraped,
            States.Completed,
            States.Failed,
            States.Paused,
        }
    ),
    States.Ongoing: frozenset(
        {
            States.Ongoing,
            States.Requested,
            States.Indexed,
            States.Scraped,
            States.Downloaded,
            States.Symlinked,
            States.Completed,
            States.PartiallyCompleted,
            States.Failed,
            States.Paused,
        }
    ),
    States.Requested: frozenset(
        {
            States.Requested,
            States.Indexed,
            States.Scraped,
            States.Downloaded,
            States.Failed,
            States.Paused,
        }
    ),
    States.Indexed: frozenset(
        {
            States.Indexed,
            States.Requested,
            States.Scraped,
            States.Downloaded,
            States.Completed,
            States.Failed,
            States.Paused,
        }
    ),
    States.Scraped: frozenset(
        {
            States.Scraped,
            States.Indexed,
            States.Requested,
            States.Downloaded,
            States.Completed,
            States.Failed,
            States.Paused,
        }
    ),
    States.Downloaded: frozenset(
        {
            States.Downloaded,
            States.Symlinked,
            States.Completed,
            States.Scraped,
            States.Failed,
            States.Paused,
        }
    ),
    States.Symlinked: frozenset(
        {
            States.Symlinked,
            States.Completed,
            States.Failed,
            States.Paused,
        }
    ),
    States.PartiallyCompleted: frozenset(
        {
            States.PartiallyCompleted,
            States.Ongoing,
            States.Requested,
            States.Indexed,
            States.Scraped,
            States.Downloaded,
            States.Completed,
            States.Failed,
            States.Paused,
        }
    ),
    States.Completed: frozenset(
        {
            States.Completed,
            States.Requested,
            States.Indexed,
            States.Scraped,
            States.Downloaded,
            States.PartiallyCompleted,
            States.Failed,
            States.Paused,
        }
    ),
    States.Failed: frozenset(
        {
            States.Failed,
            States.Requested,
            States.Indexed,
            States.Scraped,
            States.Downloaded,
            States.Paused,
        }
    ),
    States.Paused: frozenset(
        {
            States.Paused,
            States.Requested,
            States.Indexed,
            States.Scraped,
            States.Downloaded,
            States.Ongoing,
            States.PartiallyCompleted,
            States.Completed,
            States.Failed,
        }
    ),
}

# State Classifications
TERMINAL_STATES: Final[frozenset[States]] = frozenset(
    {
        States.Completed,
        States.Failed,
        States.Paused,
    }
)

ACTIVE_PROCESSING_STATES: Final[frozenset[States]] = frozenset(
    {
        States.Requested,
        States.Indexed,
        States.Scraped,
        States.Downloaded,
        States.Symlinked,
    }
)

RETRYABLE_STATES: Final[frozenset[States]] = frozenset(
    {
        States.Failed,
        States.Requested,
        States.Indexed,
        States.Scraped,
        States.Ongoing,
        States.PartiallyCompleted,
    }
)


def can_transition(from_state: States, to_state: States) -> bool:
    """Return True if transitioning from `from_state` to `to_state` is logically permissible."""
    valid_targets = ALLOWED_TRANSITIONS.get(from_state)
    if valid_targets is None:
        return False
    return to_state in valid_targets


def is_terminal_state(state: States) -> bool:
    """Return True if the state is considered terminal (processing stops unless explicitly re-triggered)."""
    return state in TERMINAL_STATES


def is_retryable_state(state: States) -> bool:
    """Return True if an item in this state can be retried by the background scheduler/retry engine."""
    return state in RETRYABLE_STATES


def is_active_state(state: States) -> bool:
    """Return True if the state indicates in-flight pipeline execution."""
    return state in ACTIVE_PROCESSING_STATES
