import asyncio
from weakref import WeakKeyDictionary


class ItemLock:
    """
    Per-item async locking utility scoped to the active asyncio event loop.

    This provides synchronization for operations on specific items (e.g., metadata sync)
    to prevent race conditions without binding locks across different event loops.
    """

    _loop_locks: WeakKeyDictionary[
        asyncio.AbstractEventLoop, dict[int, asyncio.Lock]
    ] = WeakKeyDictionary()
    _global_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
        WeakKeyDictionary()
    )

    @classmethod
    async def get_lock(cls, item_id: int) -> asyncio.Lock:
        """Get the lock for a specific item id in the current running event loop."""
        loop = asyncio.get_running_loop()
        global_lock = cls._global_locks.get(loop)
        if global_lock is None:
            global_lock = asyncio.Lock()
            cls._global_locks[loop] = global_lock

        async with global_lock:
            locks = cls._loop_locks.setdefault(loop, {})
            lock = locks.get(item_id)
            if lock is None:
                lock = asyncio.Lock()
                locks[item_id] = lock
            return lock

    @classmethod
    async def acquire(cls, item_id: int, timeout: float | None = None) -> bool:
        """
        Attempt to acquire the lock for a specific item.

        Args:
            item_id: The ID of the item to lock.
            timeout: Maximum time to wait for the lock in seconds.

        Returns:
            bool: True if lock acquired, False otherwise (timeout).
        """
        lock = await cls.get_lock(item_id)
        if timeout is not None:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
                return True
            except TimeoutError:
                return False

        await lock.acquire()
        return True

    @classmethod
    def release(cls, item_id: int) -> None:
        """Release the lock for a specific item in the current running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        locks = cls._loop_locks.get(loop)
        if not locks:
            return
        lock = locks.get(item_id)
        if lock is not None and lock.locked():
            lock.release()
