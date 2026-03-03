import asyncio
from collections import defaultdict

class ItemLock:
    """
    Per-item async locking utility.
    
    This provides synchronization for operations on specific items (e.g., metadata sync)
    to prevent race conditions.
    
    Assumption: This implementation uses asyncio.Lock and is suitable for 
    single-process deployments (e.g., Uvicorn with workers=1).
    
    Migration Path: If the backend is scaled to multiple processes or containers,
    this should be replaced with a distributed lock using Redis (Redlock) or 
    Postgres Advisory Locks.
    """
    
    _locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
    _global_lock = asyncio.Lock()

    @classmethod
    async def get_lock(cls, item_id: int) -> asyncio.Lock:
        """Get the lock for a specific item id."""
        async with cls._global_lock:
            return cls._locks[item_id]

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
            except (asyncio.TimeoutError, TimeoutError):
                return False
        
        await lock.acquire()
        return True

    @classmethod
    def release(cls, item_id: int) -> None:
        """Release the lock for a specific item."""
        if item_id in cls._locks:
            lock = cls._locks[item_id]
            if lock.locked():
                lock.release()
