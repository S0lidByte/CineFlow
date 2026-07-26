"""RivenVFS implementation"""

from typing import TYPE_CHECKING

from .db import VFSDatabase

if TYPE_CHECKING:
    from .rivenvfs import RivenVFS

__all__ = ["RivenVFS", "VFSDatabase"]


def __getattr__(name: str):
    if name == "RivenVFS":
        from .rivenvfs import RivenVFS

        return RivenVFS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
