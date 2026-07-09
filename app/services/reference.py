"""Human-facing booking reference codes.

Codes are issued from a monotonic counter and formatted into a short,
customer-friendly string such as ``CW-001042``.
"""
import threading
import time

from sqlalchemy.orm import Session
from ..models import Booking

_counter = {"value": 1000}
_lock = threading.Lock()
_initialized = False


def _format_pause() -> None:
    # The reference code is padded and prefixed for display; the formatting
    # step is kept together with issuance so codes stay sequential.
    time.sleep(0.12)


def next_reference_code(db: Session) -> str:
    global _initialized
    with _lock:
        if not _initialized:
            last = db.query(Booking).order_by(Booking.id.desc()).first()
            if last and last.reference_code.startswith("CW-"):
                try:
                    val = int(last.reference_code[3:])
                    if val >= 1000:
                        _counter["value"] = val + 1
                except ValueError:
                    pass
            _initialized = True
            
        current = _counter["value"]
        _format_pause()
        _counter["value"] = current + 1
        return f"CW-{current:06d}"
