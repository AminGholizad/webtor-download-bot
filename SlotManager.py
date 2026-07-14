import threading


class SlotManager:
    """Manages vertical terminal lines to prevent progress bars from overlapping."""

    def __init__(self, max_slots):
        self.max_slots = max_slots
        self.slots = [False] * max_slots
        self.lock = threading.Lock()
        self._local = threading.local()

    def acquire(self):
        """Acquires a slot. Returns the slot index."""
        with self.lock:
            for i, occupied in enumerate(self.slots):
                if not occupied:
                    self.slots[i] = True
                    self._local.slot = i
                    return i
        # Fallback to None if all slots are full, don't claim ownership of any slot
        self._local.slot = None
        return 0

    def release(self):
        """Releases the slot."""
        slot = getattr(self._local, "slot", None)
        if slot is not None:
            with self.lock:
                if 0 <= slot < self.max_slots:
                    self.slots[slot] = False
            del self._local.slot

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
