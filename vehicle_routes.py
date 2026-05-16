from flask import Blueprint, render_template

from db import get_db


vehicle_bp = Blueprint("vehicle_routes", __name__)


@vehicle_bp.route("/availability")
def availability():
    db = get_db()
    vehicles = db.execute("""
        SELECT
            v.id,
            v.make || ' ' || v.model AS vehicle_name,
            v.license_plate,
            v.status,
            MIN(CASE
                WHEN b.status IN ('Awaiting Payment','Confirmed')
                     AND b.return_date >= date('now')
                THEN b.pickup_date
            END) AS next_booking_start,
            MIN(CASE
                WHEN b.status IN ('Awaiting Payment','Confirmed')
                     AND b.return_date >= date('now')
                THEN b.return_date
            END) AS next_booking_return
        FROM vehicles v
        LEFT JOIN bookings b ON b.vehicle_id = v.id
        GROUP BY v.id
        ORDER BY v.category, v.make, v.model
    """).fetchall()
    db.close()
    return render_template("availability_calendar.html", vehicles=vehicles)
