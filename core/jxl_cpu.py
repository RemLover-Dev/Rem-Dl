"""Portable conversion thread budget; a share of cores, not a CPU usage quota."""
import os

MAX_THREADS = 1024


def available_cpu_count():
    counts = []
    for getter in (getattr(os, 'process_cpu_count', None), os.cpu_count):
        if getter is not None:
            try:
                count = getter()
                if isinstance(count, int) and count > 0:
                    counts.append(count)
            except (OSError, NotImplementedError):
                pass
    affinity = getattr(os, 'sched_getaffinity', None)
    if affinity is not None:
        try:
            count = len(affinity(0))
            if count:
                counts.append(count)
        except (OSError, NotImplementedError):
            pass
    return min(counts) if counts else 1


def conversion_threads():
    # Round down to leave at least the remaining quarter of reported logical
    # cores free on multi-core machines. A one-core machine still needs a worker.
    return max(1, min(MAX_THREADS, available_cpu_count() * 3 // 4))


def parallel_threads(file_count, memory_bytes):
    """At most four active encoders share the original total CPU thread budget."""
    total = conversion_threads()
    workers = max(1, min(4, total, max(1, file_count), max(1, memory_bytes // (256 * 1024 * 1024))))
    quotient, remainder = divmod(total, workers)
    return [quotient + (index < remainder) for index in range(workers)]
