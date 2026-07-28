from typing import TYPE_CHECKING

from .cache import Cache, CacheConfig
from .chunker import ChunkCacheNotifier
from .exceptions.chunk_exception import (
    ChunkException,
    ChunksTooSlowException,
)
from .exceptions.media_stream_data_exception import (
    ByteLengthMismatchException,
    CacheDataNotFoundException,
    EmptyDataException,
    MediaStreamDataException,
)
from .exceptions.media_stream_exception import MediaStreamException

if TYPE_CHECKING:
    from .media_stream import MediaStream

__all__ = [
    "ByteLengthMismatchException",
    "Cache",
    "CacheConfig",
    "CacheDataNotFoundException",
    "ChunkCacheNotifier",
    "ChunkException",
    "ChunksTooSlowException",
    "EmptyDataException",
    "MediaStream",
    "MediaStreamDataException",
    "MediaStreamException",
]


def __getattr__(name: str):
    if name == "MediaStream":
        from .media_stream import MediaStream

        return MediaStream

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
