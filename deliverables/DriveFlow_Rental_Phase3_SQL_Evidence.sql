-- DriveFlow Rental Phase 3 SQL Evidence
-- Raw SQL statements for physical design, database objects, and query requirements.

-- Complete schema block from db.py: tables, constraints, indexes, and views

CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            license_number TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'customer' CHECK (role IN ('admin','staff','customer')),
            loyalty_points INTEGER DEFAULT 0 CHECK (loyalty_points >= 0),
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vin TEXT UNIQUE NOT NULL,
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            year INTEGER NOT NULL,
            license_plate TEXT UNIQUE NOT NULL,
            category TEXT NOT NULL CHECK (category IN ('Economy','SUV','Luxury')),
            daily_rate REAL NOT NULL CHECK (daily_rate > 0),
            status TEXT NOT NULL DEFAULT 'Available' CHECK (status IN ('Available','Rented','Maintenance')),
            mileage INTEGER DEFAULT 0 CHECK (mileage >= 0),
            last_service_mileage INTEGER DEFAULT 0 CHECK (last_service_mileage >= 0),
            next_service_date TEXT,
            image_url TEXT
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
            pickup_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            total_amount REAL NOT NULL CHECK (total_amount >= 0),
            discount_amount REAL DEFAULT 0 CHECK (discount_amount >= 0),
            promo_code TEXT,
            deposit_amount REAL DEFAULT 0 CHECK (deposit_amount >= 0),
            deposit_status TEXT DEFAULT 'None',
            status TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending','Awaiting Payment','Confirmed','Cancelled','Returned')),
            created_at TEXT DEFAULT (datetime('now'))
            CHECK (julianday(return_date) > julianday(pickup_date))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            amount REAL NOT NULL CHECK (amount >= 0),
            method TEXT NOT NULL,
            card_last4 TEXT,
            reference TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'Paid' CHECK (status IN ('Paid','Pending','Failed')),
            paid_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS penalties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            type TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL CHECK (amount >= 0),
            days_late INTEGER DEFAULT 0 CHECK (days_late >= 0),
            status TEXT NOT NULL DEFAULT 'Unpaid' CHECK (status IN ('Unpaid','Paid','Waived')),
            issued_by INTEGER REFERENCES users(id),
            issued_at TEXT DEFAULT (datetime('now')),
            paid_at TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS returns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            actual_return_date TEXT NOT NULL,
            condition TEXT NOT NULL DEFAULT 'Good' CHECK (condition IN ('Good','Damaged')),
            notes TEXT,
            days_late INTEGER DEFAULT 0 CHECK (days_late >= 0),
            return_mileage INTEGER DEFAULT 0 CHECK (return_mileage >= 0),
            processed_by INTEGER REFERENCES users(id),
            processed_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            description TEXT,
            discount_type TEXT NOT NULL DEFAULT 'percent' CHECK (discount_type IN ('percent','flat')),
            discount_value REAL NOT NULL CHECK (discount_value > 0),
            min_booking_amount REAL DEFAULT 0 CHECK (min_booking_amount >= 0),
            max_uses INTEGER DEFAULT 100 CHECK (max_uses > 0),
            uses INTEGER DEFAULT 0 CHECK (uses >= 0),
            active INTEGER DEFAULT 1 CHECK (active IN (0,1)),
            expires_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            comment TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS maintenance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
            type TEXT NOT NULL,
            description TEXT,
            cost REAL DEFAULT 0 CHECK (cost >= 0),
            mileage_at_service INTEGER DEFAULT 0 CHECK (mileage_at_service >= 0),
            service_date TEXT NOT NULL,
            next_service_date TEXT,
            next_service_mileage INTEGER,
            performed_by TEXT,
            logged_by INTEGER REFERENCES users(id),
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id INTEGER,
            detail TEXT,
            ip_address TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS loyalty_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            points INTEGER NOT NULL,
            type TEXT NOT NULL,
            description TEXT,
            booking_id INTEGER REFERENCES bookings(id),
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS waitlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
            requested_pickup TEXT NOT NULL,
            requested_return TEXT NOT NULL,
            status TEXT DEFAULT 'Waiting' CHECK (status IN ('Waiting','Notified','Closed')),
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
        CREATE INDEX IF NOT EXISTS idx_vehicles_category_status ON vehicles(category, status);
        CREATE INDEX IF NOT EXISTS idx_bookings_user_status ON bookings(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_bookings_vehicle_dates ON bookings(vehicle_id, pickup_date, return_date);
        CREATE INDEX IF NOT EXISTS idx_payments_booking ON payments(booking_id);
        CREATE INDEX IF NOT EXISTS idx_penalties_booking_status ON penalties(booking_id, status);
        CREATE INDEX IF NOT EXISTS idx_reviews_vehicle ON reviews(vehicle_id);
        CREATE INDEX IF NOT EXISTS idx_maintenance_vehicle_date ON maintenance_logs(vehicle_id, service_date);

        CREATE VIEW IF NOT EXISTS vw_available_vehicles AS
        SELECT id, make, model, year, license_plate, category, daily_rate, mileage, image_url
        FROM vehicles
        WHERE status = 'Available';

        CREATE VIEW IF NOT EXISTS vw_booking_summary AS
        SELECT b.id AS booking_id, u.name AS customer_name, u.email AS customer_email,
               v.make || ' ' || v.model AS vehicle_name, v.category,
               b.pickup_date, b.return_date, b.total_amount, b.discount_amount, b.status
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        JOIN vehicles v ON v.id = b.vehicle_id;

        CREATE VIEW IF NOT EXISTS vw_revenue_by_category AS
        SELECT v.category, COUNT(b.id) AS total_bookings,
               COALESCE(SUM(b.total_amount), 0) AS total_revenue,
               COALESCE(ROUND(AVG(b.total_amount), 2), 0) AS average_booking_value
        FROM vehicles v
        LEFT JOIN bookings b ON b.vehicle_id = v.id AND b.status IN ('Confirmed', 'Returned')
        GROUP BY v.category;

        CREATE VIEW IF NOT EXISTS vw_customer_penalty_balance AS
        SELECT u.id AS user_id, u.name, u.email,
               COUNT(CASE WHEN p.status = 'Unpaid' THEN 1 END) AS unpaid_penalties,
               COALESCE(SUM(CASE WHEN p.status = 'Unpaid' THEN p.amount ELSE 0 END), 0) AS outstanding_amount
        FROM users u
        LEFT JOIN bookings b ON b.user_id = u.id
        LEFT JOIN penalties p ON p.booking_id = b.id
        WHERE u.role = 'customer'
        GROUP BY u.id, u.name, u.email;

-- Query evidence mapped to Phase 3 requirements

-- Core table creation example
CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT UNIQUE NOT NULL,
    make TEXT NOT NULL,
    model TEXT NOT NULL,
    year INTEGER NOT NULL,
    license_plate TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('Economy','SUV','Luxury')),
    daily_rate REAL NOT NULL CHECK (daily_rate > 0),
    status TEXT NOT NULL DEFAULT 'Available'
        CHECK (status IN ('Available','Rented','Maintenance')),
    mileage INTEGER DEFAULT 0 CHECK (mileage >= 0),
    last_service_mileage INTEGER DEFAULT 0 CHECK (last_service_mileage >= 0),
    next_service_date TEXT,
    image_url TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_vehicles_category_status
ON vehicles(category, status);

CREATE INDEX IF NOT EXISTS idx_bookings_vehicle_dates
ON bookings(vehicle_id, pickup_date, return_date);

CREATE INDEX IF NOT EXISTS idx_bookings_user_status
ON bookings(user_id, status);

-- Views
CREATE VIEW IF NOT EXISTS vw_booking_summary AS
SELECT b.id AS booking_id, u.name AS customer_name, u.email AS customer_email,
       v.make || ' ' || v.model AS vehicle_name, v.category,
       b.pickup_date, b.return_date, b.total_amount, b.discount_amount, b.status
FROM bookings b
JOIN users u ON u.id = b.user_id
JOIN vehicles v ON v.id = b.vehicle_id;

-- Query 1: vehicle search with LIKE, AND, OR, sorting, and row limitation
SELECT id, make, model, year, category, daily_rate
FROM vehicles
WHERE status != 'Maintenance'
  AND (make LIKE :search_term OR model LIKE :search_term)
ORDER BY category, daily_rate
LIMIT 20;

-- Query 2: date-based availability conflict check
SELECT id
FROM bookings
WHERE vehicle_id = :vehicle_id
  AND status NOT IN ('Cancelled','Returned')
  AND NOT (return_date <= :pickup_date OR pickup_date >= :return_date);

-- Query 3: revenue by category with aggregate, rounding, GROUP BY, and HAVING
SELECT v.category,
       COUNT(b.id) AS total_bookings,
       ROUND(SUM(b.total_amount), 2) AS total_revenue,
       ROUND(AVG(b.total_amount), 2) AS average_booking_value
FROM vehicles v
JOIN bookings b ON b.vehicle_id = v.id
WHERE b.status IN ('Confirmed','Returned')
GROUP BY v.category
HAVING COUNT(b.id) >= 1
ORDER BY total_revenue DESC;

-- Query 4: customer penalty balance with join and sub-query
SELECT u.name, u.email,
       (SELECT COALESCE(SUM(p.amount), 0)
        FROM penalties p
        JOIN bookings b2 ON b2.id = p.booking_id
        WHERE b2.user_id = u.id AND p.status = 'Unpaid') AS outstanding_balance
FROM users u
WHERE u.role = 'customer'
ORDER BY outstanding_balance DESC
LIMIT 10;

-- Query 5: date functions for maintenance due soon
SELECT make, model, license_plate, next_service_date
FROM vehicles
WHERE next_service_date IS NOT NULL
  AND next_service_date <= date('now', '+30 days')
ORDER BY next_service_date ASC;

-- Query 6: character functions and variable-style parameter for promo validation
SELECT code, description, discount_type, discount_value
FROM promo_codes
WHERE UPPER(code) = UPPER(TRIM(:promo_code))
  AND active = 1
  AND uses < max_uses
  AND (expires_at IS NULL OR expires_at > datetime('now'));
