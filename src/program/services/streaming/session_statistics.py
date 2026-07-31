from dataclasses import dataclass


@dataclass
class SessionStatistics:
    """Statistics about the current streaming session."""

    bytes_transferred: int = 0
    total_session_connections: int = 0
    # Incremented only for 'body_read' type reads (sequential HTTP fetches).
    # cache_hit / general_scan / footer_scan do NOT increment this.
    # Used by has_active_streams() to distinguish genuine user playback from
    # Plex intro/credit detection which may do 1–2 non-sequential fetches.
    body_read_count: int = 0
    # Monotonic timestamp of the last body_read. Used by has_active_streams()
    # to filter out idle streams (e.g. completed Plex intro scans or paused players).
    last_body_read_timestamp: float = 0.0
