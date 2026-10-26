"""Deduplicate bounded background work without waiting under a registry lock."""

def submit_shared(pool, pending, lock, key, function, *, max_pending=None):
    """Return the shared Future, or None when optional background work is full.

    Callers use an RLock because an already-completed Future invokes its done
    callback synchronously during registration. Foreground work omits the cap.
    """
    with lock:
        future = pending.get(key)
        if future is not None:
            return future
        if max_pending is not None and len(pending) >= max_pending:
            return None
        future = pool.submit(function, key)
        pending[key] = future

        def finished(completed):
            with lock:
                if pending.get(key) is completed:
                    pending.pop(key, None)

        future.add_done_callback(finished)
        return future
