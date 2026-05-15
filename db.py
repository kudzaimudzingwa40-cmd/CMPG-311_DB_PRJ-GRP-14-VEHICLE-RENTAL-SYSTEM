import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), "driveflow.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            license_number TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'customer',
            loyalty_points INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vin TEXT UNIQUE NOT NULL,
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            year INTEGER NOT NULL,
            license_plate TEXT UNIQUE NOT NULL,
            category TEXT NOT NULL,
            daily_rate REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'Available',
            mileage INTEGER DEFAULT 0,
            last_service_mileage INTEGER DEFAULT 0,
            next_service_date TEXT,
            image_url TEXT
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
            pickup_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            total_amount REAL NOT NULL,
            discount_amount REAL DEFAULT 0,
            promo_code TEXT,
            deposit_amount REAL DEFAULT 0,
            deposit_status TEXT DEFAULT 'None',
            status TEXT NOT NULL DEFAULT 'Pending',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            amount REAL NOT NULL,
            method TEXT NOT NULL,
            card_last4 TEXT,
            reference TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'Paid',
            paid_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS penalties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            type TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            days_late INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Unpaid',
            issued_by INTEGER REFERENCES users(id),
            issued_at TEXT DEFAULT (datetime('now')),
            paid_at TEXT,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS returns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL REFERENCES bookings(id),
            actual_return_date TEXT NOT NULL,
            condition TEXT NOT NULL DEFAULT 'Good',
            notes TEXT,
            days_late INTEGER DEFAULT 0,
            return_mileage INTEGER DEFAULT 0,
            processed_by INTEGER REFERENCES users(id),
            processed_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            description TEXT,
            discount_type TEXT NOT NULL DEFAULT 'percent',
            discount_value REAL NOT NULL,
            min_booking_amount REAL DEFAULT 0,
            max_uses INTEGER DEFAULT 100,
            uses INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
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
            cost REAL DEFAULT 0,
            mileage_at_service INTEGER DEFAULT 0,
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
            status TEXT DEFAULT 'Waiting',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)

   
    if not c.execute("SELECT id FROM users WHERE email='admin@driveflow.com'").fetchone():
        c.execute("INSERT INTO users (name,email,password_hash,license_number,role) VALUES (?,?,?,?,?)",
            ("Admin","admin@driveflow.com",generate_password_hash("admin123"),"ADMIN-000","admin"))

   
    if not c.execute("SELECT id FROM users WHERE email='staff@driveflow.com'").fetchone():
        c.execute("INSERT INTO users (name,email,password_hash,license_number,role) VALUES (?,?,?,?,?)",
            ("Fleet Manager","staff@driveflow.com",generate_password_hash("staff123"),"STAFF-001","staff"))

   
    if c.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0] == 0:
        vehicles = [
            ("VIN001","Toyota","Corolla",2022,"CA-001-GP","Economy",450.00,"Available",45000),
            ("VIN002","Toyota","Camry",2023,"CA-002-GP","Economy",550.00,"Available",12000),
            ("VIN003","Honda","Civic",2022,"CA-003-GP","Economy",480.00,"Available",38000),
            ("VIN004","Ford","Explorer",2023,"CA-004-GP","SUV",850.00,"Available",22000),
            ("VIN005","Toyota","Fortuner",2023,"CA-005-GP","SUV",950.00,"Rented",18000),
            ("VIN006","Nissan","Pathfinder",2022,"CA-006-GP","SUV",900.00,"Available",31000),
            ("VIN007","BMW","5 Series",2023,"CA-007-GP","Luxury",1800.00,"Available",8000),
            ("VIN008","Mercedes","C-Class",2023,"CA-008-GP","Luxury",2000.00,"Available",5500),
            ("VIN009","Audi","A6",2022,"CA-009-GP","Luxury",1900.00,"Maintenance",41000),
            ("VIN010","Volkswagen","Polo",2023,"CA-010-GP","Economy",420.00,"Available",15000),
        ]
        c.executemany("INSERT INTO vehicles (vin,make,model,year,license_plate,category,daily_rate,status,mileage) VALUES (?,?,?,?,?,?,?,?,?)", vehicles)

    image_updates = [
        ("https://commons.wikimedia.org/wiki/Special:FilePath/Toyota%20Corolla%202.0%20XEi%202022.jpg?width=1200", "Toyota", "Corolla"),
        ("https://commons.wikimedia.org/wiki/Special:FilePath/Toyota%20Camry%202.5%20Hybrid%20%282023%29%20%2853130732285%29.jpg?width=1200", "Toyota", "Camry"),
        ("https://commons.wikimedia.org/wiki/Special:FilePath/2022%20Honda%20Civic.jpg?width=1200", "Honda", "Civic"),
        ("https://commons.wikimedia.org/wiki/Special:FilePath/2023%20Ford%20Explorer.jpg?width=1200", "Ford", "Explorer"),
        ("https://commons.wikimedia.org/wiki/Special:FilePath/Toyota%20Fortuner%202.4%20G%204x2%202023.jpg?width=1200", "Toyota", "Fortuner"),
        ("https://commons.wikimedia.org/wiki/Special:FilePath/2022%20Nissan%20Pathfinder%20SV.jpg?width=1200", "Nissan", "Pathfinder"),
        ("https://commons.wikimedia.org/wiki/Special:FilePath/BMW%205-Series%20%28G30%29%20530d%20xDrive%20%282023%29%20%2853333798201%29.jpg?width=1200", "BMW", "5 Series"),
        ("https://commons.wikimedia.org/wiki/Special:FilePath/Mercedes-Benz%20C-Klasse%20%28W206%29%20C%20300%20%282023%29%20%2853491181737%29.jpg?width=1200", "Mercedes", "C-Class"),
        ("https://commons.wikimedia.org/wiki/Special:FilePath/Audi%20A6%20C8.jpg?width=1200", "Audi", "A6"),
        ("https://commons.wikimedia.org/wiki/Special:FilePath/2023%20Volkswagen%20Polo%20Track%201.6%20MSi.jpg?width=1200", "Volkswagen", "Polo"),
    ]
    # Replace the original generic Unsplash seeds while preserving admin-provided image URLs.
    c.executemany(
        "UPDATE vehicles SET image_url=? WHERE make=? AND model=? AND (image_url IS NULL OR image_url='' OR image_url LIKE 'https://images.unsplash.com/%')",
        image_updates
    )

    if c.execute("SELECT COUNT(*) FROM promo_codes").fetchone()[0] == 0:
        c.executemany("INSERT INTO promo_codes (code,description,discount_type,discount_value,min_booking_amount,max_uses) VALUES (?,?,?,?,?,?)",[
            ("WELCOME10","10% off for new customers","percent",10,0,500),
            ("FLAT200","R200 off bookings over R1000","flat",200,1000,100),
            ("VIP25","25% loyalty discount","percent",25,500,50),
        ])

    conn.commit()
    conn.close()

def audit(user_id, action, entity, entity_id=None, detail=None, ip=None):
    db = get_db()
    db.execute("INSERT INTO audit_logs (user_id,action,entity,entity_id,detail,ip_address) VALUES (?,?,?,?,?,?)",
        (user_id, action, entity, entity_id, detail, ip))
    db.commit()
    db.close()
