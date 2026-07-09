"""Administrative reporting and export endpoints."""
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import cache
from ..auth import require_admin
from ..database import get_db
from ..errors import AppError
from ..models import Booking, Room, User
from ..services.export import generate_export
from ..timeutils import parse_input_datetime

router = APIRouter(prefix="/admin", tags=["admin"])


def _parse_report_range(frm: str, to: str) -> tuple[datetime, datetime, bool]:
    try:
        from_date = datetime.strptime(frm, "%Y-%m-%d").date()
        to_date = datetime.strptime(to, "%Y-%m-%d").date()
    except ValueError:
        try:
            return parse_input_datetime(frm), parse_input_datetime(to), True
        except AppError:
            raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid date range")
    return datetime.combine(from_date, time.min), datetime.combine(to_date + timedelta(days=1), time.min), False


@router.get("/usage-report")
def usage_report(
    frm: str = Query(..., alias="from"),
    to: str = Query(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    cached = cache.get_report(admin.org_id, frm, to)
    if cached is not None:
        return cached

    range_start, range_end, exact_end = _parse_report_range(frm, to)
    if range_end < range_start:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid date range")

    rooms = db.query(Room).filter(Room.org_id == admin.org_id).order_by(Room.id.asc()).all()
    room_rows = []
    for room in rooms:
        query = db.query(Booking).filter(
            Booking.room_id == room.id,
            Booking.status == "confirmed",
            Booking.start_time >= range_start,
        )
        if exact_end:
            query = query.filter(Booking.start_time <= range_end)
        else:
            query = query.filter(Booking.start_time < range_end)
        bookings = query.all()
        room_rows.append(
            {
                "room_id": room.id,
                "room_name": room.name,
                "confirmed_bookings": len(bookings),
                "revenue_cents": sum(b.price_cents for b in bookings),
            }
        )

    result = {"from": frm, "to": to, "rooms": room_rows}
    cache.set_report(admin.org_id, frm, to, result)
    return result


@router.get("/export")
def export(
    room_id: int | None = Query(None),
    include_all: bool = Query(False),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if room_id is not None:
        room = db.query(Room).filter(Room.id == room_id, Room.org_id == admin.org_id).first()
        if room is None:
            raise AppError(404, "ROOM_NOT_FOUND", "Room not found")
    csv_body = generate_export(db, admin.org_id, admin.id, room_id, include_all)
    return Response(content=csv_body, media_type="text/csv")
