from collections.abc import Awaitable
from pathlib import Path

class InodeT(int): ...
class FileHandleT(int): ...
class ModeT(int): ...
class FileNameT(bytes): ...

ROOT_INODE: InodeT
default_options: set[str]

class FUSEError(OSError):
    errno: int | None
    def __init__(self, errno: int) -> None: ...

class Operations: ...

class RequestContext:
    uid: int
    gid: int
    pid: int
    umask: int

class EntryAttributes:
    st_ino: InodeT
    generation: int
    entry_timeout: float
    attr_timeout: float
    st_mode: ModeT
    st_nlink: int
    st_uid: int
    st_gid: int
    st_rdev: int
    st_size: int
    st_blksize: int
    st_blocks: int
    st_atime_ns: int
    st_mtime_ns: int
    st_ctime_ns: int

class FileInfo:
    fh: FileHandleT
    keep_cache: bool
    direct_io: bool
    nonseekable: bool
    def __init__(self, *, fh: FileHandleT = ...) -> None: ...

class ReaddirToken: ...

def init(
    operations: Operations,
    mountpoint: str | bytes | Path,
    options: set[str],
) -> None: ...
def close(*, unmount: bool = ...) -> None: ...
def terminate() -> None: ...
def invalidate_inode(inode: InodeT, *, attr_only: bool = ...) -> None: ...
def invalidate_entry_async(
    parent_inode: InodeT,
    name: FileNameT,
    *,
    deleted: InodeT = ...,
    ignore_enoent: bool = ...,
) -> Awaitable[None]: ...
def readdir_reply(
    token: ReaddirToken,
    name: FileNameT,
    attrs: EntryAttributes,
    next_id: int,
) -> bool: ...
def main() -> Awaitable[None]: ...
