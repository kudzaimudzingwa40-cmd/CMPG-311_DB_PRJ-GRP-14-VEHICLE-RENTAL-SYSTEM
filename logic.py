from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, jsonify, make_response)
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_db, init_db, audit
from vehicle_routes import vehicle_bp
from datetime import datetime, date
from functools import wraps
import os, string, random, csv, io

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "driveflow-secret-2026")
app.register_blueprint(vehicle_bp)

# ── Decorators ──────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") not in ("admin",):
            flash("Admin access required.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") not in ("admin", "staff"):
            flash("Staff access required.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated

def customer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "customer":
            flash("Use a customer account to rent vehicles.", "error")
            if session.get("role") == "admin":
                return redirect(url_for("admin_dashboard"))
            if session.get("role") == "staff":
                return redirect(url_for("staff_fleet"))
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def gen_ref(prefix="DRV"):
    return prefix + "-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))

# FIX #5: Loyalty points were manually patched in session across many routes,
# which meant any code path that forgot to update session["points"] would show
# a stale counter in the navbar until the customer re-logged.
# Solution: refresh points from the DB once per request for logged-in customers.
@app.before_request
def refresh_session_points():
    if session.get("role") == "customer" and session.get("user_id"):
        db = get_db()
        row = db.execute("SELECT loyalty_points FROM users WHERE id=?",
                         (session["user_id"],)).fetchone()
        db.close()
        if row:
            session["points"] = row["loyalty_points"]

def parse_iso_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None

def has_booking_conflict(db, vehicle_id, pickup, ret, exclude_booking_id=None):
    query = """
        SELECT id FROM bookings
        WHERE vehicle_id=? AND status NOT IN ('Cancelled','Returned')
        AND NOT (return_date <= ? OR pickup_date >= ?)
    """
    params = [vehicle_id, pickup, ret]
    if exclude_booking_id:
        query += " AND id != ?"
        params.append(exclude_booking_id)
    return db.execute(query, params).fetchone() is not None

def sync_vehicle_status(db, vehicle_id):
    vehicle = db.execute("SELECT status FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if not vehicle or vehicle["status"] == "Maintenance":
        return
    today = date.today().isoformat()
    active_now = db.execute("""
        SELECT id FROM bookings
        WHERE vehicle_id=? AND status='Confirmed'
        AND pickup_date <= ? AND return_date > ?
        LIMIT 1
    """, (vehicle_id, today, today)).fetchone()
    new_status = "Rented" if active_now else "Available"
    db.execute("UPDATE vehicles SET status=? WHERE id=?", (new_status, vehicle_id))

    # FIX #7: When a vehicle becomes Available, advance any 'Waiting' waitlist
    # entries whose requested dates are still in the future to 'Notified'.
    # In a production system this would trigger an email; here we mark the
    # record so the customer can see it on their dashboard.
    if new_status == "Available":
        db.execute("""
            UPDATE waitlist SET status='Notified'
            WHERE vehicle_id=? AND status='Waiting'
            AND requested_pickup >= ?
        """, (vehicle_id, today))

# ── Public ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    db = get_db()
    _vs = db.execute("""
        SELECT COUNT(*) as total,
               SUM(status='Available') as available,
               COUNT(DISTINCT category) as categories
        FROM vehicles
    """).fetchone()
    stats = {
        "total_vehicles": _vs["total"],
        "available":      _vs["available"],
        "categories":     _vs["categories"],
    }
    featured = db.execute("SELECT * FROM vehicles WHERE status='Available' ORDER BY daily_rate DESC LIMIT 3").fetchall()
    db.close()
    return render_template("index.html", stats=stats, featured=featured)

@app.route("/vehicles")
def vehicles():
    category = request.args.get("category","")
    search   = request.args.get("search","")
    pickup   = request.args.get("pickup_date","").strip()
    ret      = request.args.get("return_date","").strip()
    p_date   = parse_iso_date(pickup)
    r_date   = parse_iso_date(ret)
    date_error = None
    dates_valid = False
    if pickup or ret:
        if not pickup or not ret or not p_date or not r_date:
            date_error = "Choose a valid pickup and return date to filter availability."
        elif p_date < date.today():
            date_error = "Pickup date cannot be in the past."
        elif r_date <= p_date:
            date_error = "Return date must be after pickup date."
        else:
            dates_valid = True
    db = get_db()
    q = "SELECT v.*, COALESCE(ROUND(AVG(r.rating),1),0) as avg_rating, COUNT(r.id) as review_count FROM vehicles v LEFT JOIN reviews r ON r.vehicle_id=v.id WHERE v.status != 'Maintenance'"
    params = []
    if category: q += " AND v.category=?"; params.append(category)
    if search:   q += " AND (v.make LIKE ? OR v.model LIKE ?)"; params += [f"%{search}%",f"%{search}%"]
    if dates_valid:
        q += """
            AND NOT EXISTS (
                SELECT 1 FROM bookings b
                WHERE b.vehicle_id=v.id AND b.status NOT IN ('Cancelled','Returned')
                AND NOT (b.return_date <= ? OR b.pickup_date >= ?)
            )
        """
        params += [pickup, ret]
    q += " GROUP BY v.id ORDER BY v.category, v.daily_rate"
    cars = db.execute(q, params).fetchall()
    db.close()
    if date_error:
        flash(date_error, "error")
    return render_template("vehicles.html", vehicles=cars, selected_category=category,
        search=search, pickup_date=pickup, return_date=ret, dates_valid=dates_valid)

@app.route("/vehicles/<int:vehicle_id>")
def vehicle_detail(vehicle_id):
    db = get_db()
    vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if not vehicle:
        db.close()
        flash("Vehicle not found.", "error")
        return redirect(url_for("vehicles"))
    reviews = db.execute("""
        SELECT rv.*, u.name as customer_name FROM reviews rv
        JOIN users u ON rv.user_id=u.id
        WHERE rv.vehicle_id=? ORDER BY rv.created_at DESC
    """, (vehicle_id,)).fetchall()
    avg = db.execute("SELECT COALESCE(ROUND(AVG(rating),1),0) FROM reviews WHERE vehicle_id=?", (vehicle_id,)).fetchone()[0]
    db.close()
    return render_template("vehicle_detail.html", vehicle=vehicle, reviews=reviews, avg_rating=avg)

# ── Auth ─────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        pwd   = request.form["password"]
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        db.close()
        if user and check_password_hash(user["password_hash"], pwd):
            session.update({"user_id":user["id"],"name":user["name"],"role":user["role"],"points":user["loyalty_points"]})
            audit(user["id"],"LOGIN","users",user["id"],f"Login from {request.remote_addr}",request.remote_addr)
            flash(f"Welcome back, {user['name']}!", "success")
            if user["role"] == "admin":  return redirect(url_for("admin_dashboard"))
            if user["role"] == "staff":  return redirect(url_for("staff_fleet"))
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        name    = request.form["name"].strip()
        email   = request.form["email"].strip().lower()
        license = request.form["license"].strip()
        pwd     = request.form["password"]
        if len(pwd) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")
        if not name or not license:
            flash("Name and license number are required.", "error")
            return render_template("register.html")
        db = get_db()
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            flash("Email already registered.", "error"); db.close()
            return render_template("register.html")
        db.execute("INSERT INTO users (name,email,password_hash,license_number) VALUES (?,?,?,?)",
            (name, email, generate_password_hash(pwd), license))
        uid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Welcome loyalty points
        db.execute("INSERT INTO loyalty_transactions (user_id,points,type,description) VALUES (?,?,?,?)",
            (uid, 100, "welcome", "Welcome bonus — 100 points"))
        db.execute("UPDATE users SET loyalty_points=100 WHERE id=?", (uid,))
        db.commit(); db.close()
        audit(uid,"REGISTER","users",uid,"New customer registration")
        flash("Account created! You've earned 100 welcome points. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/logout")
def logout():
    uid = session.get("user_id")
    if uid: audit(uid,"LOGOUT","users",uid)
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))

# ── Customer Dashboard ───────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
@customer_required
def dashboard():
    db = get_db()
    bookings = db.execute("""
        SELECT b.*, v.make, v.model, v.year, v.license_plate, v.category, v.daily_rate,
               (SELECT COUNT(*) FROM penalties p WHERE p.booking_id=b.id AND p.status='Unpaid') as unpaid_count,
               (SELECT COALESCE(SUM(p.amount),0) FROM penalties p WHERE p.booking_id=b.id AND p.status='Unpaid') as unpaid_total,
               (SELECT COUNT(*) FROM reviews rv WHERE rv.booking_id=b.id) as has_review
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.user_id=? ORDER BY b.created_at DESC
    """, (session["user_id"],)).fetchall()
    all_penalties = db.execute("""
        SELECT p.*, v.make, v.model FROM penalties p
        JOIN bookings b ON p.booking_id=b.id JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.user_id=? AND p.status='Unpaid' ORDER BY p.issued_at DESC
    """, (session["user_id"],)).fetchall()
    points_history = db.execute(
        "SELECT * FROM loyalty_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
        (session["user_id"],)).fetchall()
    # FIX #7: Surface waitlist notifications so the customer can see which
    # vehicles have become available for their requested dates.
    waitlist_notifications = db.execute("""
        SELECT w.*, v.make, v.model, v.category, v.daily_rate
        FROM waitlist w JOIN vehicles v ON w.vehicle_id=v.id
        WHERE w.user_id=? AND w.status='Notified'
        ORDER BY w.created_at DESC
    """, (session["user_id"],)).fetchall()
    user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    db.close()
    return render_template("dashboard.html", bookings=bookings,
        all_penalties=all_penalties, points_history=points_history,
        waitlist_notifications=waitlist_notifications, user=user)

# ── Profile ──────────────────────────────────────────────────────────────────
@app.route("/profile", methods=["GET","POST"])
@login_required
@customer_required
def profile():
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_info":
            name    = request.form["name"].strip()
            license = request.form["license"].strip()
            db.execute("UPDATE users SET name=?, license_number=? WHERE id=?",
                (name, license, session["user_id"]))
            session["name"] = name
            audit(session["user_id"],"UPDATE_PROFILE","users",session["user_id"])
            flash("Profile updated.", "success")
        elif action == "change_password":
            current = request.form["current_password"]
            new_pwd = request.form["new_password"]
            if not check_password_hash(user["password_hash"], current):
                flash("Current password is incorrect.", "error")
            elif len(new_pwd) < 6:
                flash("New password must be at least 6 characters.", "error")
            else:
                db.execute("UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(new_pwd), session["user_id"]))
                audit(session["user_id"],"CHANGE_PASSWORD","users",session["user_id"])
                flash("Password changed successfully.", "success")
        db.commit()
        user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    db.close()
    return render_template("profile.html", user=user)

# ── Promo code validator ─────────────────────────────────────────────────────
@app.route("/api/promo/<code>")
@login_required
@customer_required
def validate_promo(code):
    db = get_db()
    promo = db.execute("""
        SELECT * FROM promo_codes WHERE code=? AND active=1
        AND (expires_at IS NULL OR expires_at > datetime('now'))
        AND uses < max_uses
    """, (code.upper(),)).fetchone()
    db.close()
    if promo:
        return jsonify({"valid":True,"type":promo["discount_type"],"value":promo["discount_value"],
                        "description":promo["description"],"min_amount":promo["min_booking_amount"]})
    return jsonify({"valid":False})

# ── Book ─────────────────────────────────────────────────────────────────────
@app.route("/book/<int:vehicle_id>", methods=["GET","POST"])
@login_required
@customer_required
def book(vehicle_id):
    db = get_db()
    vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    user    = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if not vehicle or vehicle["status"] == "Maintenance":
        flash("Vehicle not available.", "error"); db.close()
        return redirect(url_for("vehicles"))

    if request.method == "POST":
        pickup = request.form["pickup_date"].strip()
        ret = request.form["return_date"].strip()
        p_date = parse_iso_date(pickup)
        r_date = parse_iso_date(ret)
        promo_code  = request.form.get("promo_code","").strip().upper()
        use_points  = request.form.get("use_points") == "1"
        use_deposit = request.form.get("use_deposit") == "1"

        if not p_date or not r_date:
            flash("Choose valid pickup and return dates.", "error")
        elif p_date < date.today():
            flash("Pickup date cannot be in the past.", "error")
        elif r_date <= p_date:
            flash("Return date must be after pickup date.", "error")
        else:
            if has_booking_conflict(db, vehicle_id, pickup, ret):
                flash("Vehicle already booked for those dates.", "error")
            else:
                days  = (r_date - p_date).days
                total = days * vehicle["daily_rate"]
                discount = 0; promo_used = None

                # Apply promo
                if promo_code:
                    promo = db.execute("""
                        SELECT * FROM promo_codes WHERE code=? AND active=1
                        AND (expires_at IS NULL OR expires_at > datetime('now')) AND uses < max_uses
                    """, (promo_code,)).fetchone()
                    if promo and total >= promo["min_booking_amount"]:
                        discount = (total * promo["discount_value"]/100) if promo["discount_type"]=="percent" else promo["discount_value"]
                        discount = min(discount, total)
                        promo_used = promo_code
                        db.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (promo_code,))
                    else:
                        flash("Promo code is invalid, expired, or below its minimum booking amount.", "error")
                        db.close()
                        return render_template("book.html", vehicle=vehicle, user=user,
                            min_date=date.today().isoformat())

                # Apply loyalty points (100 points = R10)
                points_used = 0
                if use_points and user["loyalty_points"] >= 100:
                    max_discount = (user["loyalty_points"] // 100) * 10
                    points_discount = min(max_discount, total - discount)
                    points_used = int(points_discount / 10) * 100
                    discount += points_discount

                final_total = max(0, total - discount)
                deposit = round(final_total * 0.2, 2) if use_deposit and final_total > 0 else 0

                db.execute("""
                    INSERT INTO bookings (user_id,vehicle_id,pickup_date,return_date,
                        total_amount,discount_amount,promo_code,deposit_amount,deposit_status,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (session["user_id"], vehicle_id, pickup, ret, final_total,
                      discount, promo_used, deposit,
                      "Pending" if deposit > 0 else "None", "Awaiting Payment"))
                bid = db.execute("SELECT last_insert_rowid()").fetchone()[0]

                # Deduct loyalty points
                if points_used > 0:
                    db.execute("UPDATE users SET loyalty_points=loyalty_points-? WHERE id=?",
                        (points_used, session["user_id"]))
                    db.execute("INSERT INTO loyalty_transactions (user_id,points,type,description,booking_id) VALUES (?,?,?,?,?)",
                        (session["user_id"], -points_used, "redeem", f"Points redeemed for booking #{bid}", bid))

                db.commit(); db.close()
                audit(session["user_id"],"CREATE_BOOKING","bookings",bid,f"Vehicle {vehicle_id}, {pickup} to {ret}")
                flash(f"Booking created! {'Discount applied: R'+str(round(discount,2))+'. ' if discount>0 else ''}Please complete payment.", "info")
                return redirect(url_for("payment", booking_id=bid))

    db.close()
    return render_template("book.html", vehicle=vehicle, user=user,
        min_date=date.today().isoformat())

# ── Cancel booking ───────────────────────────────────────────────────────────
@app.route("/cancel/<int:booking_id>", methods=["POST"])
@login_required
@customer_required
def cancel_booking(booking_id):
    db = get_db()
    b = db.execute("SELECT * FROM bookings WHERE id=? AND user_id=?", (booking_id, session["user_id"])).fetchone()
    pickup_date = parse_iso_date(b["pickup_date"]) if b else None
    if not b:
        flash("Booking not found.", "error")
    elif b["status"] not in ("Confirmed","Awaiting Payment"):
        flash("Only unpaid or confirmed bookings can be cancelled.", "error")
    elif b["status"] == "Confirmed" and pickup_date and pickup_date <= date.today():
        flash("This rental has already started. Please contact staff for return support.", "error")
    else:
        db.execute("UPDATE bookings SET status='Cancelled' WHERE id=?", (booking_id,))
        sync_vehicle_status(db, b["vehicle_id"])
        redeemed = db.execute("""
            SELECT COALESCE(SUM(-points),0) FROM loyalty_transactions
            WHERE booking_id=? AND user_id=? AND type='redeem' AND points < 0
        """, (booking_id, session["user_id"])).fetchone()[0]
        if redeemed > 0:
            db.execute("UPDATE users SET loyalty_points=loyalty_points+? WHERE id=?",
                (redeemed, session["user_id"]))
            db.execute("INSERT INTO loyalty_transactions (user_id,points,type,description,booking_id) VALUES (?,?,?,?,?)",
                (session["user_id"], redeemed, "refund", "Points refunded for cancelled booking", booking_id))
        db.commit()
        audit(session["user_id"],"CANCEL_BOOKING","bookings",booking_id)
        flash("Booking cancelled.", "success")
    db.close()
    return redirect(url_for("dashboard"))

# ── Extend booking ───────────────────────────────────────────────────────────
@app.route("/extend/<int:booking_id>", methods=["GET","POST"])
@login_required
@customer_required
def extend_booking(booking_id):
    db = get_db()
    b = db.execute("""
        SELECT b.*, v.make, v.model, v.daily_rate, v.license_plate
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.id=? AND b.user_id=? AND b.status='Confirmed'
    """, (booking_id, session["user_id"])).fetchone()
    if not b:
        flash("Booking not found or not eligible for extension.", "error")
        db.close(); return redirect(url_for("dashboard"))

    if request.method == "POST":
        new_return = request.form["new_return_date"]
        new_date   = parse_iso_date(new_return)
        old_date   = parse_iso_date(b["return_date"])
        if not new_date or not old_date:
            flash("Choose a valid return date.", "error")
        elif new_date <= old_date:
            flash("New return date must be after current return date.", "error")
        else:
            # Check for conflicts after original return date
            if has_booking_conflict(db, b["vehicle_id"], b["return_date"], new_return, booking_id):
                flash("Vehicle is already booked for those extended dates by another customer.", "error")
            else:
                extra_days = (new_date - old_date).days
                extra_cost = extra_days * b["daily_rate"]
                db.execute("UPDATE bookings SET return_date=?, total_amount=total_amount+? WHERE id=?",
                    (new_return, extra_cost, booking_id))
                # FIX #4: Sync vehicle status after extension — the new return date
                # may change whether the vehicle is currently 'Rented' or 'Available'.
                sync_vehicle_status(db, b["vehicle_id"])
                db.commit()
                audit(session["user_id"],"EXTEND_BOOKING","bookings",booking_id,f"Extended to {new_return}, +R{extra_cost}")
                db.close()
                flash(f"Booking extended to {new_return}. Additional charge: R{extra_cost:,.2f}", "success")
                return redirect(url_for("dashboard"))
    db.close()
    return render_template("extend_booking.html", booking=b)

# ── Waitlist ─────────────────────────────────────────────────────────────────
@app.route("/waitlist/<int:vehicle_id>", methods=["POST"])
@login_required
@customer_required
def join_waitlist(vehicle_id):
    pickup = request.form["pickup_date"].strip()
    ret = request.form["return_date"].strip()
    p_date = parse_iso_date(pickup)
    r_date = parse_iso_date(ret)
    if not p_date or not r_date or r_date <= p_date or p_date < date.today():
        flash("Choose valid future dates before joining the waitlist.", "error")
        return redirect(url_for("vehicles"))
    db = get_db()
    existing = db.execute("""
        SELECT id FROM waitlist
        WHERE user_id=? AND vehicle_id=? AND requested_pickup=? AND requested_return=? AND status='Waiting'
    """, (session["user_id"], vehicle_id, pickup, ret)).fetchone()
    if not existing:
        db.execute("INSERT INTO waitlist (user_id,vehicle_id,requested_pickup,requested_return) VALUES (?,?,?,?)",
            (session["user_id"], vehicle_id, pickup, ret))
    db.commit(); db.close()
    flash("You've been added to the waitlist. We'll notify you when this vehicle becomes available.", "success")
    return redirect(url_for("dashboard"))

# ── Payment ──────────────────────────────────────────────────────────────────
@app.route("/pay/<int:booking_id>", methods=["GET","POST"])
@login_required
@customer_required
def payment(booking_id):
    db = get_db()
    b = db.execute("""
        SELECT b.*, v.make, v.model, v.year, v.license_plate, v.category, v.daily_rate
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.id=? AND b.user_id=?
    """, (booking_id, session["user_id"])).fetchone()
    if not b:
        flash("Booking not found.", "error"); db.close(); return redirect(url_for("dashboard"))
    if b["status"] != "Awaiting Payment":
        flash("This booking has already been paid or cancelled.", "info"); db.close(); return redirect(url_for("dashboard"))

    amount_due = b["deposit_amount"] if b["deposit_amount"] and b["deposit_status"] == "Pending" else b["total_amount"]

    if request.method == "POST":
        method     = request.form.get("method","Card")
        if method not in ("Card", "EFT", "Cash"):
            flash("Choose a valid payment method.", "error")
            db.close()
            return render_template("payment.html", booking=b, amount_due=amount_due)

        # FIX #6: Validate all payment methods, not just Card.
        card_num = "".join(ch for ch in request.form.get("card_number","") if ch.isdigit())
        if method == "Card" and not (12 <= len(card_num) <= 19):
            flash("Enter a valid card number for card payments.", "error")
            db.close()
            return render_template("payment.html", booking=b, amount_due=amount_due)
        eft_ref = request.form.get("eft_reference","").strip()
        if method == "EFT" and not eft_ref:
            flash("Enter your EFT proof-of-payment reference number.", "error")
            db.close()
            return render_template("payment.html", booking=b, amount_due=amount_due)
        cash_confirm = request.form.get("cash_confirm","")
        if method == "Cash" and cash_confirm != "1":
            flash("Please confirm that cash has been handed over at the counter.", "error")
            db.close()
            return render_template("payment.html", booking=b, amount_due=amount_due)

        card_last4 = card_num[-4:] if card_num else None
        reference  = gen_ref("DRV")
        db.execute("UPDATE bookings SET status='Confirmed' WHERE id=?", (booking_id,))
        if b["deposit_amount"] and b["deposit_status"] == "Pending":
            db.execute("UPDATE bookings SET deposit_status='Paid' WHERE id=?", (booking_id,))
        sync_vehicle_status(db, b["vehicle_id"])
        db.execute("INSERT INTO payments (booking_id,amount,method,card_last4,reference,status) VALUES (?,?,?,?,?,'Paid')",
            (booking_id, amount_due, method, card_last4, reference))
        # Earn loyalty points (1 point per R10 spent)
        points_earned = int(amount_due / 10)
        if points_earned > 0:
            db.execute("UPDATE users SET loyalty_points=loyalty_points+? WHERE id=?",
                (points_earned, session["user_id"]))
            db.execute("INSERT INTO loyalty_transactions (user_id,points,type,description,booking_id) VALUES (?,?,?,?,?)",
                (session["user_id"], points_earned, "earn", f"Points earned for booking #{booking_id}", booking_id))
        db.commit(); db.close()
        audit(session["user_id"],"PAYMENT","payments",booking_id,f"R{amount_due} via {method}")
        flash(f"Payment successful! You earned {points_earned} loyalty points.", "success")
        return redirect(url_for("receipt", booking_id=booking_id))
    db.close()
    return render_template("payment.html", booking=b, amount_due=amount_due)

# ── Receipt ──────────────────────────────────────────────────────────────────
@app.route("/receipt/<int:booking_id>")
@login_required
@customer_required
def receipt(booking_id):
    db = get_db()
    b = db.execute("""
        SELECT b.*, v.make, v.model, v.year, v.license_plate, v.category, v.daily_rate,
               u.name as customer_name, u.email as customer_email, u.license_number
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id JOIN users u ON b.user_id=u.id
        WHERE b.id=? AND b.user_id=?
    """, (booking_id, session["user_id"])).fetchone()
    payment = db.execute("SELECT * FROM payments WHERE booking_id=?", (booking_id,)).fetchone()
    db.close()
    if not b or not payment:
        flash("Receipt not found.", "error"); return redirect(url_for("dashboard"))
    return render_template("receipt.html", booking=b, payment=payment)

# ── Reviews ──────────────────────────────────────────────────────────────────
@app.route("/review/<int:booking_id>", methods=["GET","POST"])
@login_required
@customer_required
def submit_review(booking_id):
    db = get_db()
    b = db.execute("""
        SELECT b.*, v.make, v.model, v.year FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.id=? AND b.user_id=? AND b.status='Returned'
    """, (booking_id, session["user_id"])).fetchone()
    if not b:
        flash("You can only review returned bookings.", "error"); db.close()
        return redirect(url_for("dashboard"))
    existing = db.execute("SELECT id FROM reviews WHERE booking_id=?", (booking_id,)).fetchone()
    if existing:
        flash("You have already reviewed this booking.", "info"); db.close()
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        rating  = int(request.form["rating"])
        comment = request.form.get("comment","").strip()
        db.execute("INSERT INTO reviews (booking_id,user_id,vehicle_id,rating,comment) VALUES (?,?,?,?,?)",
            (booking_id, session["user_id"], b["vehicle_id"], rating, comment))
        # Bonus points for review
        db.execute("UPDATE users SET loyalty_points=loyalty_points+25 WHERE id=?", (session["user_id"],))
        db.execute("INSERT INTO loyalty_transactions (user_id,points,type,description,booking_id) VALUES (?,?,?,?,?)",
            (session["user_id"], 25, "review", "Bonus points for leaving a review", booking_id))
        db.commit(); db.close()
        flash("Review submitted! You earned 25 bonus points. Thank you!", "success")
        return redirect(url_for("dashboard"))
    db.close()
    return render_template("submit_review.html", booking=b)

# ── Penalties (customer) ─────────────────────────────────────────────────────
@app.route("/penalties")
@login_required
@customer_required
def my_penalties():
    db = get_db()
    penalties = db.execute("""
        SELECT p.*, v.make, v.model, v.year, v.license_plate, v.category,
               b.pickup_date, b.return_date, b.id as booking_ref
        FROM penalties p JOIN bookings b ON p.booking_id=b.id JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.user_id=? ORDER BY p.issued_at DESC
    """, (session["user_id"],)).fetchall()
    totals = db.execute("""
        SELECT COALESCE(SUM(CASE WHEN p.status='Unpaid' THEN p.amount ELSE 0 END),0) as outstanding,
               COALESCE(SUM(CASE WHEN p.status='Paid'   THEN p.amount ELSE 0 END),0) as paid_total,
               COUNT(CASE WHEN p.status='Unpaid' THEN 1 END) as unpaid_count
        FROM penalties p JOIN bookings b ON p.booking_id=b.id WHERE b.user_id=?
    """, (session["user_id"],)).fetchone()
    db.close()
    return render_template("my_penalties.html", penalties=penalties, totals=totals)

@app.route("/penalties/pay/<int:penalty_id>", methods=["GET","POST"])
@login_required
@customer_required
def pay_penalty(penalty_id):
    db = get_db()
    penalty = db.execute("""
        SELECT p.*, v.make, v.model, v.year, v.license_plate, v.category,
               b.pickup_date, b.return_date, b.id as booking_ref,
               u.name as customer_name, u.email as customer_email, u.license_number
        FROM penalties p JOIN bookings b ON p.booking_id=b.id
        JOIN vehicles v ON b.vehicle_id=v.id JOIN users u ON b.user_id=u.id
        WHERE p.id=? AND b.user_id=?
    """, (penalty_id, session["user_id"])).fetchone()
    if not penalty or penalty["status"] != "Unpaid":
        flash("Penalty not found or already settled.", "info"); db.close()
        return redirect(url_for("my_penalties"))
    if request.method == "POST":
        method = request.form.get("method","Card")
        card_num = request.form.get("card_number","").replace(" ","")
        reference = gen_ref("PEN")
        db.execute("UPDATE penalties SET status='Paid', paid_at=datetime('now'), notes=? WHERE id=?",
            (f"Paid via {method}. Ref: {reference}", penalty_id))
        db.commit()
        db.close()
        audit(session["user_id"],"PAY_PENALTY","penalties",penalty_id,f"R{penalty['amount']} via {method}")
        flash(f"Payment of R{penalty['amount']:,.2f} successful!", "success")
        return render_template("penalty_receipt.html", penalty=penalty, method=method,
                               card_last4=card_num[-4:] if card_num else "0000", reference=reference)
    db.close()
    return render_template("pay_penalty.html", penalty=penalty)

# ── Export bookings CSV ──────────────────────────────────────────────────────
@app.route("/export/bookings")
@login_required
@customer_required
def export_my_bookings():
    db = get_db()
    rows = db.execute("""
        SELECT b.id, v.make||' '||v.model as vehicle, v.license_plate,
               b.pickup_date, b.return_date, b.total_amount, b.discount_amount, b.status, b.created_at
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.user_id=? ORDER BY b.created_at DESC
    """, (session["user_id"],)).fetchall()
    db.close()
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["Booking #","Vehicle","Plate","Pickup","Return","Amount (R)","Discount (R)","Status","Booked On"])
    for r in rows:
        w.writerow(list(r))
    resp = make_response(output.getvalue())
    resp.headers["Content-Disposition"] = "attachment; filename=my_bookings.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp


@app.route("/staff")
@login_required
@staff_required
def staff_fleet():
    db = get_db()
    vehicles = db.execute("SELECT * FROM vehicles ORDER BY status, category").fetchall()
    maintenance = db.execute("""
        SELECT ml.*, v.make, v.model, v.license_plate
        FROM maintenance_logs ml JOIN vehicles v ON ml.vehicle_id=v.id
        ORDER BY ml.service_date DESC LIMIT 20
    """).fetchall()
    db.close()
    return render_template("staff/fleet.html", vehicles=vehicles, maintenance=maintenance)

@app.route("/staff/vehicle/<int:vehicle_id>/status", methods=["POST"])
@login_required
@staff_required
def staff_update_status(vehicle_id):
    new_status = request.form["status"]
    if new_status not in ("Available", "Rented", "Maintenance"):
        flash("Invalid vehicle status.", "error")
        return redirect(url_for("staff_fleet"))
    db = get_db()
    db.execute("UPDATE vehicles SET status=? WHERE id=?", (new_status, vehicle_id))
    db.commit(); db.close()
    audit(session["user_id"],"UPDATE_VEHICLE_STATUS","vehicles",vehicle_id,f"Status → {new_status}")
    flash(f"Status updated to {new_status}.", "success")
    return redirect(url_for("staff_fleet"))

@app.route("/staff/maintenance/add", methods=["GET","POST"])
@login_required
@staff_required
def staff_add_maintenance():
    db = get_db()
    vehicles = db.execute("SELECT id,make,model,license_plate FROM vehicles ORDER BY make").fetchall()
    if request.method == "POST":
        vid     = int(request.form["vehicle_id"])
        mtype   = request.form["type"]
        desc    = request.form.get("description","")
        cost    = float(request.form.get("cost",0) or 0)
        mileage = int(request.form.get("mileage",0) or 0)
        sdate   = request.form["service_date"]
        next_d  = request.form.get("next_service_date","") or None
        next_m  = request.form.get("next_service_mileage","") or None
        perf_by = request.form.get("performed_by","")
        db.execute("""
            INSERT INTO maintenance_logs (vehicle_id,type,description,cost,mileage_at_service,
                service_date,next_service_date,next_service_mileage,performed_by,logged_by)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (vid, mtype, desc, cost, mileage, sdate, next_d, next_m, perf_by, session["user_id"]))
        db.execute("UPDATE vehicles SET mileage=?, last_service_mileage=?, next_service_date=? WHERE id=?",
            (mileage, mileage, next_d, vid))
        if mtype == "Major Service" or mtype == "Repair":
            db.execute("UPDATE vehicles SET status='Maintenance' WHERE id=?", (vid,))
        db.commit(); db.close()
        audit(session["user_id"],"ADD_MAINTENANCE","maintenance_logs",vid,f"{mtype} on vehicle {vid}")
        flash("Maintenance log added.", "success")
        return redirect(url_for("staff_fleet"))
    db.close()
    return render_template("staff/maintenance_form.html", vehicles=vehicles)


@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    db = get_db()
    _vs = db.execute("""
        SELECT
            COUNT(*) as total,
            SUM(status='Available') as available,
            SUM(status='Rented') as rented,
            SUM(status='Maintenance') as maintenance
        FROM vehicles
    """).fetchone()
    # FIX #3: Revenue must include 'Returned' bookings — completed rentals are
    # moved to 'Returned', so counting only 'Confirmed' understates total revenue.
    _bs = db.execute("""
        SELECT
            COUNT(*) as total,
            SUM(status='Confirmed') as active,
            COALESCE(SUM(CASE WHEN status IN ('Confirmed','Returned') THEN total_amount ELSE 0 END),0) as revenue
        FROM bookings
    """).fetchone()
    stats = {
        "total_vehicles":        _vs["total"],
        "available":             _vs["available"],
        "rented":                _vs["rented"],
        "maintenance":           _vs["maintenance"],
        "total_users":           db.execute("SELECT COUNT(*) FROM users WHERE role='customer'").fetchone()[0],
        "total_bookings":        _bs["total"],
        "total_revenue":         _bs["revenue"],
        "active_bookings":       _bs["active"],
        "outstanding_penalties": db.execute("SELECT COALESCE(SUM(amount),0) FROM penalties WHERE status='Unpaid'").fetchone()[0],
        "total_reviews":         db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
    }
    recent_bookings = db.execute("""
        SELECT b.*, u.name as customer_name, v.make, v.model
        FROM bookings b JOIN users u ON b.user_id=u.id JOIN vehicles v ON b.vehicle_id=v.id
        ORDER BY b.created_at DESC LIMIT 8
    """).fetchall()
    # FIX #3: Include Returned bookings in monthly revenue chart.
    monthly_chart = db.execute("""
        SELECT strftime('%Y-%m', created_at) as month, COALESCE(SUM(total_amount),0) as revenue
        FROM bookings WHERE status IN ('Confirmed','Returned')
        GROUP BY month ORDER BY month DESC LIMIT 6
    """).fetchall()
    db.close()
    return render_template("admin/dashboard.html", stats=stats,
        recent_bookings=recent_bookings, monthly_chart=list(reversed(monthly_chart)))

# ── Admin Fleet ──────────────────────────────────────────────────────────────
@app.route("/admin/fleet")
@login_required
@admin_required
def admin_fleet():
    db = get_db()
    status_filter = request.args.get("status","")
    q = "SELECT v.*, COALESCE(ROUND(AVG(r.rating),1),0) as avg_rating FROM vehicles v LEFT JOIN reviews r ON r.vehicle_id=v.id"
    params = []
    if status_filter: q += " WHERE v.status=?"; params.append(status_filter)
    q += " GROUP BY v.id ORDER BY v.category, v.make"
    vehicles_list = db.execute(q, params).fetchall()
    db.close()
    return render_template("admin/fleet.html", vehicles=vehicles_list, status_filter=status_filter)

@app.route("/admin/fleet/add", methods=["GET","POST"])
@login_required
@admin_required
def admin_add_vehicle():
    if request.method == "POST":
        db = get_db()
        try:
            image_url = request.form.get("image_url", "").strip() or None
            db.execute("INSERT INTO vehicles (vin,make,model,year,license_plate,category,daily_rate,status,image_url) VALUES (?,?,?,?,?,?,?,?,?)",
                (request.form["vin"],request.form["make"],request.form["model"],
                 int(request.form["year"]),request.form["license_plate"],
                 request.form["category"],float(request.form["daily_rate"]),request.form["status"],image_url))
            db.commit()
            audit(session["user_id"],"ADD_VEHICLE","vehicles",None,request.form["make"]+" "+request.form["model"])
            flash("Vehicle added.", "success")
        except Exception as e: flash(f"Error: {e}", "error")
        finally: db.close()
        return redirect(url_for("admin_fleet"))
    return render_template("admin/vehicle_form.html", vehicle=None, action="Add")

@app.route("/admin/fleet/edit/<int:vehicle_id>", methods=["GET","POST"])
@login_required
@admin_required
def admin_edit_vehicle(vehicle_id):
    db = get_db()
    vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if request.method == "POST":
        image_url = request.form.get("image_url", "").strip() or None
        db.execute("UPDATE vehicles SET make=?,model=?,year=?,license_plate=?,category=?,daily_rate=?,status=?,image_url=? WHERE id=?",
            (request.form["make"],request.form["model"],int(request.form["year"]),
             request.form["license_plate"],request.form["category"],float(request.form["daily_rate"]),
             request.form["status"],image_url,vehicle_id))
        db.commit()
        audit(session["user_id"],"EDIT_VEHICLE","vehicles",vehicle_id)
        flash("Vehicle updated.", "success")
        db.close(); return redirect(url_for("admin_fleet"))
    db.close()
    return render_template("admin/vehicle_form.html", vehicle=vehicle, action="Edit")

@app.route("/admin/fleet/delete/<int:vehicle_id>", methods=["POST"])
@login_required
@admin_required
def admin_delete_vehicle(vehicle_id):
    db = get_db()
    vehicle = db.execute("SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
    if not vehicle:
        flash("Vehicle not found.", "error")
        db.close()
        return redirect(url_for("admin_fleet"))

    # FIX #8: Guard against orphaning related records.  Since FK enforcement
    # depends on runtime PRAGMA (and SQLite won't cascade-delete automatically),
    # we explicitly block deletion when dependent rows exist.
    active_bookings = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE vehicle_id=? AND status NOT IN ('Cancelled','Returned')",
        (vehicle_id,)).fetchone()[0]
    if active_bookings > 0:
        flash(f"Cannot delete — {active_bookings} active or pending booking(s) exist for this vehicle.", "error")
        db.close()
        return redirect(url_for("admin_fleet"))

    total_bookings = db.execute(
        "SELECT COUNT(*) FROM bookings WHERE vehicle_id=?", (vehicle_id,)).fetchone()[0]
    if total_bookings > 0:
        flash(f"Cannot delete — {total_bookings} historical booking record(s) are linked to this vehicle. "
              "Retire the vehicle by setting its status to Maintenance instead.", "error")
        db.close()
        return redirect(url_for("admin_fleet"))

    db.execute("DELETE FROM vehicles WHERE id=?", (vehicle_id,))
    db.commit(); db.close()
    audit(session["user_id"],"DELETE_VEHICLE","vehicles",vehicle_id)
    flash("Vehicle removed.", "success")
    return redirect(url_for("admin_fleet"))

@app.route("/admin/fleet/status/<int:vehicle_id>", methods=["POST"])
@login_required
@admin_required
def update_vehicle_status(vehicle_id):
    ns = request.form["status"]
    if ns not in ("Available", "Rented", "Maintenance"):
        flash("Invalid vehicle status.", "error")
        return redirect(url_for("admin_fleet"))
    db = get_db()
    db.execute("UPDATE vehicles SET status=? WHERE id=?", (ns, vehicle_id))
    db.commit(); db.close()
    audit(session["user_id"],"UPDATE_STATUS","vehicles",vehicle_id,f"→ {ns}")
    flash(f"Status updated to {ns}.", "success")
    return redirect(url_for("admin_fleet"))

# ── Admin Bookings ───────────────────────────────────────────────────────────
@app.route("/admin/bookings")
@login_required
@admin_required
def admin_bookings():
    status_filter = request.args.get("status","")
    db = get_db()
    q = """SELECT b.*, u.name as customer_name, u.email as customer_email,
               v.make, v.model, v.year, v.license_plate, v.category, v.daily_rate,
               (SELECT COUNT(*) FROM penalties p WHERE p.booking_id=b.id AND p.status='Unpaid') as unpaid_penalties,
               (SELECT COALESCE(SUM(p.amount),0) FROM penalties p WHERE p.booking_id=b.id AND p.status='Unpaid') as penalty_balance
        FROM bookings b JOIN users u ON b.user_id=u.id JOIN vehicles v ON b.vehicle_id=v.id"""
    params = []
    if status_filter: q += " WHERE b.status=?"; params.append(status_filter)
    q += " ORDER BY b.created_at DESC"
    bookings = db.execute(q, params).fetchall()
    db.close()
    return render_template("admin/bookings.html", bookings=bookings, status_filter=status_filter)

@app.route("/admin/bookings/<int:booking_id>")
@login_required
@admin_required
def admin_booking_detail(booking_id):
    db = get_db()
    b = db.execute("""
        SELECT b.*, u.name as customer_name, u.email as customer_email, u.license_number, u.id as uid,
               v.make, v.model, v.year, v.license_plate, v.category, v.daily_rate
        FROM bookings b JOIN users u ON b.user_id=u.id JOIN vehicles v ON b.vehicle_id=v.id WHERE b.id=?
    """, (booking_id,)).fetchone()
    if not b:
        flash("Booking not found.", "error"); db.close(); return redirect(url_for("admin_bookings"))
    payment   = db.execute("SELECT * FROM payments WHERE booking_id=?", (booking_id,)).fetchone()
    penalties = db.execute("""
        SELECT p.*, u.name as issued_by_name FROM penalties p LEFT JOIN users u ON p.issued_by=u.id
        WHERE p.booking_id=? ORDER BY p.issued_at DESC
    """, (booking_id,)).fetchall()
    ret_record = db.execute("SELECT * FROM returns WHERE booking_id=?", (booking_id,)).fetchone()
    db.close()
    return render_template("admin/booking_detail.html", booking=b, payment=payment,
        penalties=penalties, ret_record=ret_record, today=date.today().isoformat())

@app.route("/admin/bookings/<int:booking_id>/return", methods=["POST"])
@login_required
@admin_required
def admin_process_return(booking_id):
    db = get_db()
    b = db.execute("SELECT b.*, v.daily_rate, v.id as vid FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id WHERE b.id=?", (booking_id,)).fetchone()
    if not b: flash("Not found.","error"); db.close(); return redirect(url_for("admin_bookings"))
    actual_return  = request.form["actual_return_date"]
    condition      = request.form["condition"]
    notes          = request.form.get("notes","")
    late_fee_pct   = float(request.form.get("late_fee_pct",25))
    return_mileage = int(request.form.get("return_mileage",0) or 0)
    agreed_date    = datetime.strptime(b["return_date"],"%Y-%m-%d").date()
    actual_date    = datetime.strptime(actual_return,"%Y-%m-%d").date()
    days_late      = max(0,(actual_date - agreed_date).days)
    db.execute("DELETE FROM returns WHERE booking_id=?", (booking_id,))
    db.execute("INSERT INTO returns (booking_id,actual_return_date,condition,notes,days_late,return_mileage,processed_by) VALUES (?,?,?,?,?,?,?)",
        (booking_id, actual_return, condition, notes, days_late, return_mileage, session["user_id"]))
    if return_mileage > 0:
        db.execute("UPDATE vehicles SET mileage=? WHERE id=?", (return_mileage, b["vid"]))
    if days_late > 0:
        base_late = days_late * b["daily_rate"]
        late_total = round(base_late * (1 + late_fee_pct/100), 2)
        db.execute("INSERT INTO penalties (booking_id,type,description,amount,days_late,status,issued_by,notes) VALUES (?,?,?,?,?,?,?,?)",
            (booking_id,"Late Return",f"{days_late} day(s) late × R{b['daily_rate']:.2f} + {late_fee_pct:.0f}% surcharge",
             late_total, days_late, "Unpaid", session["user_id"],
             f"Agreed: {b['return_date']}. Actual: {actual_return}."))
        flash(f"Late return penalty: R{late_total:,.2f}", "error")
    if condition == "Damaged":
        dmg = float(request.form.get("damage_amount",1500) or 1500)
        db.execute("INSERT INTO penalties (booking_id,type,description,amount,status,issued_by,notes) VALUES (?,?,?,?,?,?,?)",
            (booking_id,"Damage","Vehicle returned damaged",dmg,"Unpaid",session["user_id"],notes or ""))
        flash(f"Damage fee issued: R{dmg:,.2f}", "error")
    db.execute("UPDATE bookings SET status='Returned' WHERE id=?", (booking_id,))
    db.execute("UPDATE vehicles SET status='Available' WHERE id=?", (b["vid"],))
    # Award loyalty points for clean return
    if days_late == 0 and condition == "Good":
        uid = b["user_id"]
        db.execute("UPDATE users SET loyalty_points=loyalty_points+50 WHERE id=?", (uid,))
        db.execute("INSERT INTO loyalty_transactions (user_id,points,type,description,booking_id) VALUES (?,?,?,?,?)",
            (uid, 50, "bonus", "On-time clean return bonus", booking_id))
        flash("Vehicle returned on time and clean — 50 bonus points awarded to customer.", "success")
    db.commit(); db.close()
    audit(session["user_id"],"PROCESS_RETURN","returns",booking_id,f"Condition: {condition}, Late: {days_late}d")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))

@app.route("/admin/bookings/<int:booking_id>/penalty/add", methods=["POST"])
@login_required
@admin_required
def admin_add_penalty(booking_id):
    db = get_db()
    db.execute("INSERT INTO penalties (booking_id,type,description,amount,status,issued_by,notes) VALUES (?,?,?,?,?,?,?)",
        (booking_id,request.form["type"],request.form["description"],
         float(request.form["amount"]),"Unpaid",session["user_id"],request.form.get("notes","")))
    db.commit(); db.close()
    audit(session["user_id"],"ADD_PENALTY","penalties",booking_id)
    flash(f"Penalty of R{float(request.form['amount']):,.2f} added.", "success")
    return redirect(url_for("admin_booking_detail", booking_id=booking_id))

@app.route("/admin/penalty/<int:penalty_id>/settle", methods=["POST"])
@login_required
@admin_required
def admin_settle_penalty(penalty_id):
    db = get_db()
    pen = db.execute("SELECT * FROM penalties WHERE id=?", (penalty_id,)).fetchone()
    if pen:
        db.execute("UPDATE penalties SET status='Paid', paid_at=datetime('now') WHERE id=?", (penalty_id,))
        db.commit(); flash(f"Penalty of R{pen['amount']:,.2f} settled.", "success")
    db.close()
    audit(session["user_id"],"SETTLE_PENALTY","penalties",penalty_id)
    return redirect(url_for("admin_booking_detail", booking_id=pen["booking_id"]))

@app.route("/admin/penalty/<int:penalty_id>/waive", methods=["POST"])
@login_required
@admin_required
def admin_waive_penalty(penalty_id):
    db = get_db()
    pen = db.execute("SELECT * FROM penalties WHERE id=?", (penalty_id,)).fetchone()
    if pen:
        reason = request.form.get("reason","Waived by admin")
        db.execute("UPDATE penalties SET status='Waived', notes=?, paid_at=datetime('now') WHERE id=?", (reason, penalty_id))
        db.commit(); flash(f"Penalty waived: {reason}", "info")
    db.close()
    audit(session["user_id"],"WAIVE_PENALTY","penalties",penalty_id)
    return redirect(url_for("admin_booking_detail", booking_id=pen["booking_id"]))

@app.route("/admin/penalties")
@login_required
@admin_required
def admin_all_penalties():
    db = get_db()
    sf = request.args.get("status","Unpaid")
    q = """SELECT p.*, b.pickup_date, b.return_date,
               u.name as customer_name, u.email as customer_email,
               v.make, v.model, v.license_plate, a.name as admin_name
        FROM penalties p JOIN bookings b ON p.booking_id=b.id
        JOIN users u ON b.user_id=u.id JOIN vehicles v ON b.vehicle_id=v.id
        LEFT JOIN users a ON p.issued_by=a.id"""
    params = []
    if sf and sf != "All": q += " WHERE p.status=?"; params.append(sf)
    q += " ORDER BY p.issued_at DESC"
    penalties = db.execute(q, params).fetchall()
    outstanding = db.execute("SELECT COALESCE(SUM(amount),0) FROM penalties WHERE status='Unpaid'").fetchone()[0]
    db.close()
    return render_template("admin/penalties.html", penalties=penalties, status_filter=sf, total_outstanding=outstanding)

# ── Admin Customers ──────────────────────────────────────────────────────────
@app.route("/admin/customers")
@login_required
@admin_required
def admin_customers():
    search = request.args.get("search","")
    db = get_db()
    q = """SELECT u.*, COUNT(DISTINCT b.id) AS total_bookings,
               COALESCE(SUM(CASE WHEN b.status='Confirmed' THEN b.total_amount END),0) AS total_spent,
               COUNT(CASE WHEN b.status='Confirmed' THEN 1 END) AS active_bookings,
               COUNT(CASE WHEN p.status='Unpaid' THEN 1 END) AS unpaid_penalties,
               COALESCE(SUM(CASE WHEN p.status='Unpaid' THEN p.amount END),0) AS penalty_balance
           FROM users u LEFT JOIN bookings b ON b.user_id=u.id LEFT JOIN penalties p ON p.booking_id=b.id
           WHERE u.role='customer'"""
    params = []
    if search: q += " AND (u.name LIKE ? OR u.email LIKE ? OR u.license_number LIKE ?)"; params += [f"%{search}%"]*3
    q += " GROUP BY u.id ORDER BY u.name"
    customers = db.execute(q, params).fetchall()
    db.close()
    return render_template("admin/customers.html", customers=customers, search=search)

@app.route("/admin/customers/<int:user_id>")
@login_required
@admin_required
def admin_customer_detail(user_id):
    db = get_db()
    customer = db.execute("SELECT * FROM users WHERE id=? AND role='customer'", (user_id,)).fetchone()
    if not customer: flash("Not found.","error"); db.close(); return redirect(url_for("admin_customers"))
    bookings  = db.execute("""SELECT b.*, v.make, v.model, v.year, v.license_plate, v.category,
        (SELECT COALESCE(SUM(amount),0) FROM penalties WHERE booking_id=b.id AND status='Unpaid') AS pen_balance
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id WHERE b.user_id=? ORDER BY b.created_at DESC""", (user_id,)).fetchall()
    penalties = db.execute("""SELECT p.*, v.make, v.model, b.pickup_date, b.return_date
        FROM penalties p JOIN bookings b ON p.booking_id=b.id JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.user_id=? ORDER BY p.issued_at DESC""", (user_id,)).fetchall()
    stats = db.execute("""SELECT COUNT(DISTINCT b.id) AS total_bookings,
        COALESCE(SUM(CASE WHEN b.status='Confirmed' THEN b.total_amount END),0) AS total_spent,
        COUNT(CASE WHEN b.status='Confirmed' THEN 1 END) AS active_bookings,
        COUNT(CASE WHEN b.status='Returned' THEN 1 END) AS completed,
        COALESCE(SUM(CASE WHEN p.status='Unpaid' THEN p.amount END),0) AS penalty_balance
        FROM bookings b LEFT JOIN penalties p ON p.booking_id=b.id WHERE b.user_id=?""", (user_id,)).fetchone()
    active  = db.execute("SELECT COUNT(*) FROM bookings WHERE user_id=? AND status IN ('Confirmed','Awaiting Payment')", (user_id,)).fetchone()[0]
    unpaid  = db.execute("SELECT COUNT(*) FROM penalties p JOIN bookings b ON p.booking_id=b.id WHERE b.user_id=? AND p.status='Unpaid'", (user_id,)).fetchone()[0]
    db.close()
    return render_template("admin/customer_detail.html", customer=customer, bookings=bookings,
        penalties=penalties, stats=stats, active_bookings=active, unpaid_penalties=unpaid)

@app.route("/admin/customers/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def admin_delete_customer(user_id):
    db = get_db()
    customer = db.execute("SELECT * FROM users WHERE id=? AND role='customer'", (user_id,)).fetchone()
    if not customer: flash("Not found.","error"); db.close(); return redirect(url_for("admin_customers"))
    active = db.execute("SELECT COUNT(*) FROM bookings WHERE user_id=? AND status IN ('Confirmed','Awaiting Payment')", (user_id,)).fetchone()[0]
    unpaid = db.execute("SELECT COUNT(*) FROM penalties p JOIN bookings b ON p.booking_id=b.id WHERE b.user_id=? AND p.status='Unpaid'", (user_id,)).fetchone()[0]
    if active > 0: flash(f"Cannot delete — {active} active booking(s). Resolve first.","error"); db.close(); return redirect(url_for("admin_customer_detail",user_id=user_id))
    if unpaid > 0: flash(f"Cannot delete — {unpaid} unpaid penalty/penalties. Resolve first.","error"); db.close(); return redirect(url_for("admin_customer_detail",user_id=user_id))
    action = request.form.get("action","anonymise")
    if action == "hard_delete":
        bids = [r[0] for r in db.execute("SELECT id FROM bookings WHERE user_id=?", (user_id,)).fetchall()]
        for bid in bids:
            for t in ["penalties","payments","returns"]: db.execute(f"DELETE FROM {t} WHERE booking_id=?", (bid,))
            db.execute("DELETE FROM bookings WHERE id=?", (bid,))
        db.execute("DELETE FROM reviews WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM loyalty_transactions WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
        flash(f"Customer '{customer['name']}' permanently deleted.", "success")
    else:
        import hashlib, time
        anon = hashlib.md5(f"{user_id}-{time.time()}".encode()).hexdigest()[:10]
        db.execute("UPDATE users SET name='[Deleted User]', email=?, license_number='ANONYMISED', password_hash='DELETED' WHERE id=?",
            (f"deleted-{anon}@driveflow.invalid", user_id))
        flash(f"Customer '{customer['name']}' anonymised.", "success")
    db.commit(); db.close()
    audit(session["user_id"],"DELETE_CUSTOMER","users",user_id,action)
    return redirect(url_for("admin_customers"))

# ── Admin Promo Codes ────────────────────────────────────────────────────────
@app.route("/admin/promos")
@login_required
@admin_required
def admin_promos():
    db = get_db()
    promos = db.execute("SELECT * FROM promo_codes ORDER BY created_at DESC").fetchall()
    db.close()
    return render_template("admin/promos.html", promos=promos)

@app.route("/admin/promos/add", methods=["POST"])
@login_required
@admin_required
def admin_add_promo():
    db = get_db()
    try:
        db.execute("INSERT INTO promo_codes (code,description,discount_type,discount_value,min_booking_amount,max_uses,expires_at) VALUES (?,?,?,?,?,?,?)",
            (request.form["code"].upper(), request.form["description"],
             request.form["discount_type"], float(request.form["discount_value"]),
             float(request.form.get("min_booking_amount",0) or 0),
             int(request.form.get("max_uses",100) or 100),
             request.form.get("expires_at","") or None))
        db.commit()
        audit(session["user_id"],"ADD_PROMO","promo_codes",None,request.form["code"])
        flash(f"Promo code {request.form['code'].upper()} created.", "success")
    except Exception as e: flash(f"Error: {e}","error")
    finally: db.close()
    return redirect(url_for("admin_promos"))

@app.route("/admin/promos/<int:pid>/toggle", methods=["POST"])
@login_required
@admin_required
def admin_toggle_promo(pid):
    db = get_db()
    promo = db.execute("SELECT * FROM promo_codes WHERE id=?", (pid,)).fetchone()
    if promo:
        db.execute("UPDATE promo_codes SET active=? WHERE id=?", (0 if promo["active"] else 1, pid))
        db.commit()
        flash(f"Promo {'deactivated' if promo['active'] else 'activated'}.", "success")
    db.close()
    return redirect(url_for("admin_promos"))

# ── Admin Reports ────────────────────────────────────────────────────────────
@app.route("/admin/reports")
@login_required
@admin_required
def admin_reports():
    db = get_db()
    monthly = db.execute("""SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as bookings,
        SUM(total_amount) as revenue FROM bookings WHERE status='Confirmed'
        GROUP BY month ORDER BY month DESC LIMIT 12""").fetchall()
    by_category = db.execute("""SELECT v.category, COUNT(*) as bookings, SUM(b.total_amount) as revenue
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id WHERE b.status='Confirmed'
        GROUP BY v.category""").fetchall()
    top_vehicles = db.execute("""SELECT v.make, v.model, v.license_plate, COUNT(*) as trips, SUM(b.total_amount) as revenue
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id WHERE b.status='Confirmed'
        GROUP BY v.id ORDER BY trips DESC LIMIT 5""").fetchall()
    top_customers = db.execute("""SELECT u.name, u.email, COUNT(*) as rentals, SUM(b.total_amount) as spent
        FROM bookings b JOIN users u ON b.user_id=u.id WHERE b.status='Confirmed'
        GROUP BY u.id ORDER BY spent DESC LIMIT 5""").fetchall()
    # Fleet utilisation
    utilisation = db.execute("""SELECT v.make, v.model, v.license_plate, v.category,
        COUNT(b.id) as total_bookings, v.status,
        COALESCE(ROUND(AVG(r.rating),1),0) as avg_rating
        FROM vehicles v LEFT JOIN bookings b ON b.vehicle_id=v.id AND b.status IN ('Confirmed','Returned')
        LEFT JOIN reviews r ON r.vehicle_id=v.id
        GROUP BY v.id ORDER BY total_bookings DESC""").fetchall()
    # Outstanding debt
    outstanding_debt = db.execute("""SELECT u.name, u.email,
        COUNT(CASE WHEN p.status='Unpaid' THEN 1 END) as penalty_count,
        SUM(CASE WHEN p.status='Unpaid' THEN p.amount ELSE 0 END) as total_owed
        FROM users u JOIN bookings b ON b.user_id=u.id JOIN penalties p ON p.booking_id=b.id
        WHERE p.status='Unpaid' GROUP BY u.id ORDER BY total_owed DESC""").fetchall()
    db.close()
    return render_template("admin/reports.html", monthly=monthly, by_category=by_category,
        top_vehicles=top_vehicles, top_customers=top_customers,
        utilisation=utilisation, outstanding_debt=outstanding_debt)

# ── Admin CSV Exports ────────────────────────────────────────────────────────
@app.route("/admin/export/<report_type>")
@login_required
@admin_required
def admin_export_csv(report_type):
    db = get_db()
    output = io.StringIO()
    w = csv.writer(output)
    if report_type == "bookings":
        w.writerow(["#","Customer","Vehicle","Plate","Pickup","Return","Amount","Discount","Status","Booked"])
        rows = db.execute("""SELECT b.id, u.name, v.make||' '||v.model, v.license_plate,
            b.pickup_date, b.return_date, b.total_amount, b.discount_amount, b.status, b.created_at
            FROM bookings b JOIN users u ON b.user_id=u.id JOIN vehicles v ON b.vehicle_id=v.id
            ORDER BY b.created_at DESC""").fetchall()
        fname = "all_bookings.csv"
    elif report_type == "penalties":
        w.writerow(["#","Customer","Vehicle","Type","Description","Amount","Days Late","Status","Issued","Paid"])
        rows = db.execute("""SELECT p.id, u.name, v.make||' '||v.model, p.type, p.description,
            p.amount, p.days_late, p.status, p.issued_at, p.paid_at
            FROM penalties p JOIN bookings b ON p.booking_id=b.id
            JOIN users u ON b.user_id=u.id JOIN vehicles v ON b.vehicle_id=v.id
            ORDER BY p.issued_at DESC""").fetchall()
        fname = "penalties_report.csv"
    elif report_type == "revenue":
        w.writerow(["Month","Bookings","Revenue (R)"])
        rows = db.execute("""SELECT strftime('%Y-%m', created_at) as month, COUNT(*), SUM(total_amount)
            FROM bookings WHERE status='Confirmed' GROUP BY month ORDER BY month DESC""").fetchall()
        fname = "revenue_report.csv"
    elif report_type == "fleet":
        w.writerow(["Vehicle","Plate","Category","Status","Mileage","Total Bookings","Avg Rating"])
        rows = db.execute("""SELECT v.make||' '||v.model, v.license_plate, v.category, v.status, v.mileage,
            COUNT(b.id), COALESCE(ROUND(AVG(r.rating),1),0)
            FROM vehicles v LEFT JOIN bookings b ON b.vehicle_id=v.id AND b.status IN ('Confirmed','Returned')
            LEFT JOIN reviews r ON r.vehicle_id=v.id GROUP BY v.id ORDER BY v.category""").fetchall()
        fname = "fleet_utilisation.csv"
    else:
        db.close(); return "Invalid report", 400
    for row in rows: w.writerow(list(row))
    db.close()
    audit(session["user_id"],"EXPORT_CSV","reports",None,report_type)
    resp = make_response(output.getvalue())
    resp.headers["Content-Disposition"] = f"attachment; filename={fname}"
    resp.headers["Content-Type"] = "text/csv"
    return resp

# ── Admin Maintenance ────────────────────────────────────────────────────────
@app.route("/admin/maintenance")
@login_required
@admin_required
def admin_maintenance():
    db = get_db()
    logs = db.execute("""SELECT ml.*, v.make, v.model, v.license_plate, u.name as logged_by_name
        FROM maintenance_logs ml JOIN vehicles v ON ml.vehicle_id=v.id
        LEFT JOIN users u ON ml.logged_by=u.id ORDER BY ml.service_date DESC""").fetchall()
    vehicles = db.execute("SELECT id,make,model,license_plate,mileage,next_service_date FROM vehicles ORDER BY make").fetchall()
    # Vehicles due for service
    due = db.execute("""SELECT * FROM vehicles WHERE next_service_date IS NOT NULL
        AND next_service_date <= date('now', '+30 days') ORDER BY next_service_date""").fetchall()
    db.close()
    return render_template("admin/maintenance.html", logs=logs, vehicles=vehicles, due_soon=due)

@app.route("/admin/maintenance/add", methods=["POST"])
@login_required
@admin_required
def admin_add_maintenance():
    db = get_db()
    vehicle_id = int(request.form["vehicle_id"])
    db.execute("""INSERT INTO maintenance_logs (vehicle_id,type,description,cost,mileage_at_service,
        service_date,next_service_date,next_service_mileage,performed_by,logged_by)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (vehicle_id, request.form["type"], request.form.get("description",""),
         float(request.form.get("cost",0) or 0), int(request.form.get("mileage",0) or 0),
         request.form["service_date"], request.form.get("next_service_date","") or None,
         request.form.get("next_service_mileage","") or None,
         request.form.get("performed_by",""), session["user_id"]))
    db.execute("UPDATE vehicles SET next_service_date=? WHERE id=?",
        (request.form.get("next_service_date","") or None, vehicle_id))
    db.commit(); db.close()
    audit(session["user_id"],"ADD_MAINTENANCE","maintenance_logs",vehicle_id,request.form["type"])
    flash("Maintenance log added.", "success")
    return redirect(url_for("admin_maintenance"))

# ── Admin Audit Log ──────────────────────────────────────────────────────────
@app.route("/admin/audit")
@login_required
@admin_required
def admin_audit():
    db = get_db()
    page    = int(request.args.get("page",1))
    per_pg  = 50
    offset  = (page-1)*per_pg
    entity_filter = request.args.get("entity","")
    q = """SELECT al.*, u.name as user_name FROM audit_logs al LEFT JOIN users u ON al.user_id=u.id"""
    params = []
    if entity_filter: q += " WHERE al.entity=?"; params.append(entity_filter)
    q += " ORDER BY al.created_at DESC LIMIT ? OFFSET ?"
    params += [per_pg, offset]
    logs = db.execute(q, params).fetchall()
    total = db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    entities = db.execute("SELECT DISTINCT entity FROM audit_logs ORDER BY entity").fetchall()
    db.close()
    return render_template("admin/audit.html", logs=logs, page=page,
        per_pg=per_pg, total=total, entity_filter=entity_filter, entities=entities)

# ── Admin Invoice PDF ────────────────────────────────────────────────────────
@app.route("/admin/invoice/<int:booking_id>")
@login_required
@admin_required
def generate_invoice(booking_id):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    db = get_db()
    b = db.execute("""SELECT b.*, v.make, v.model, v.year, v.license_plate, v.category, v.daily_rate,
        u.name as customer_name, u.email as customer_email, u.license_number
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id JOIN users u ON b.user_id=u.id WHERE b.id=?""", (booking_id,)).fetchone()
    payment   = db.execute("SELECT * FROM payments WHERE booking_id=?", (booking_id,)).fetchone()
    penalties = db.execute("SELECT * FROM penalties WHERE booking_id=? AND status='Paid'", (booking_id,)).fetchall()
    db.close()
    if not b: return "Booking not found", 404
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=2*cm, bottomMargin=2*cm, leftMargin=2*cm, rightMargin=2*cm)
    styles = getSampleStyleSheet()
    gold = colors.HexColor("#C9A84C"); dark = colors.HexColor("#111111")
    title_style  = ParagraphStyle("title", parent=styles["Heading1"], textColor=gold, fontSize=22)
    head_style   = ParagraphStyle("head",  parent=styles["Normal"],   textColor=gold, fontSize=11, spaceAfter=2)
    normal_style = ParagraphStyle("norm",  parent=styles["Normal"],   fontSize=10)
    story = []
    story.append(Paragraph("DriveFlow Rental", title_style))
    story.append(Paragraph("Premium Vehicle Rental Services", normal_style))
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph(f"<b>TAX INVOICE</b>   #INV-{booking_id:05d}", head_style))
    story.append(Spacer(1, 0.3*cm))
    # Customer & booking info table
    info_data = [
        ["Customer:", b["customer_name"], "Invoice Date:", datetime.now().strftime("%Y-%m-%d")],
        ["Email:", b["customer_email"], "Booking Ref:", f"#BK-{booking_id}"],
        ["License No.:", b["license_number"], "Payment Ref:", payment["reference"] if payment else "—"],
        ["Vehicle:", f"{b['year']} {b['make']} {b['model']}", "Plate:", b["license_plate"]],
        ["Pickup:", b["pickup_date"], "Return:", b["return_date"]],
    ]
    t = Table(info_data, colWidths=[3.5*cm,6*cm,3.5*cm,6*cm])
    t.setStyle(TableStyle([("FONTSIZE",  (0,0),(-1,-1),9),
                            ("TEXTCOLOR",(0,0),(0,-1),gold),
                            ("TEXTCOLOR",(2,0),(2,-1),gold),
                            ("FONTNAME", (0,0),(0,-1),"Helvetica-Bold"),
                            ("FONTNAME", (2,0),(2,-1),"Helvetica-Bold"),
                            ("ROWBACKGROUNDS",(0,0),(-1,-1),[colors.HexColor("#1a1a1a"),colors.HexColor("#141414")]),
                            ("TEXTCOLOR",(1,0),(-1,-1),colors.white),
                            ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#333333"))]))
    story += [t, Spacer(1, 0.5*cm)]
    # Line items
    from datetime import datetime as dt
    pickup_d = dt.strptime(b["pickup_date"],"%Y-%m-%d").date()
    return_d = dt.strptime(b["return_date"],"%Y-%m-%d").date()
    days = (return_d - pickup_d).days
    items = [["Description","Days","Unit Price","Amount"],
             [f"{b['make']} {b['model']} Rental ({b['category']})", str(days), f"R{b['daily_rate']:.2f}", f"R{days*b['daily_rate']:.2f}"]]
    if b["discount_amount"] and b["discount_amount"] > 0:
        items.append(["Discount Applied", "", "", f"-R{b['discount_amount']:.2f}"])
    for pen in penalties:
        items.append([f"Penalty: {pen['type']}", "", "", f"R{pen['amount']:.2f}"])
    items.append(["", "", "TOTAL DUE", f"R{b['total_amount']:.2f}"])
    it = Table(items, colWidths=[9*cm,2*cm,3.5*cm,3.5*cm])
    it.setStyle(TableStyle([("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
                             ("BACKGROUND",(0,0),(-1,0),gold),
                             ("TEXTCOLOR",(0,0),(-1,0),dark),
                             ("ROWBACKGROUNDS",(0,1),(-1,-2),[colors.HexColor("#1a1a1a"),colors.HexColor("#141414")]),
                             ("TEXTCOLOR",(0,1),(-1,-2),colors.white),
                             ("BACKGROUND",(0,-1),(-1,-1),colors.HexColor("#1a1a1a")),
                             ("TEXTCOLOR",(2,-1),(-1,-1),gold),
                             ("FONTNAME",(2,-1),(-1,-1),"Helvetica-Bold"),
                             ("FONTSIZE",(2,-1),(-1,-1),12),
                             ("GRID",(0,0),(-1,-1),0.3,colors.HexColor("#333333")),
                             ("ALIGN",(1,0),(-1,-1),"RIGHT")]))
    story += [it, Spacer(1,0.5*cm)]
    story.append(Paragraph("Thank you for choosing DriveFlow Rental. Please retain this invoice for your records.", normal_style))
    doc.build(story)
    buf.seek(0)
    resp = make_response(buf.read())
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f"inline; filename=invoice-BK{booking_id}.pdf"
    return resp

init_db()

if __name__ == "__main__":
    app.run(debug=True)
