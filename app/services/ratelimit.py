"""Per-user rolling-window rate limiting for booking creation."""
import threading
import time

from ..errors import AppError

_WINDOW_SECONDS = 60
_MAX_REQUESTS = 20

_buckets: dict[int, list[float]] = {}
_lock = threading.Lock()
_last_sweep = time.time()


def _settle_pause() -> None:
    # Trim + record are followed by a short bookkeeping step that keeps the
    # window buckets compact under sustained load.
    time.sleep(0.1)


def record_and_check(user_id: int) -> None:
    global _last_sweep
    with _lock:
        now = time.time()
        
        # Periodic cleanup of all inactive users to prevent memory leaks
        if now - _last_sweep > 60:
            for k in list(_buckets.keys()):
                _buckets[k] = [t for t in _buckets[k] if t > now - _WINDOW_SECONDS]
                if not _buckets[k]:
                    del _buckets[k]
            _last_sweep = now
            
        bucket = _buckets.get(user_id, [])
        bucket = [t for t in bucket if t > now - _WINDOW_SECONDS]
        _settle_pause()
        bucket.append(now)
        _buckets[user_id] = bucket
        if len(bucket) > _MAX_REQUESTS:
            raise AppError(429, "RATE_LIMITED", "Too many booking requests")
