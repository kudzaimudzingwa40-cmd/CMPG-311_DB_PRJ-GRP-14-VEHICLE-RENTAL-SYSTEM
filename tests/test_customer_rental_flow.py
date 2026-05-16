import importlib
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta


ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class CustomerRentalFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        import db

        db.DB_PATH = os.path.join(cls.tmp.name, "driveflow-test.db")
        sys.modules.pop("logic", None)
        cls.logic = importlib.import_module("logic")
        cls.app = cls.logic.app
        cls.app.config.update(TESTING=True, SECRET_KEY="test-secret")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.client = self.app.test_client()

    def login(self, email, password):
        return self.client.post("/login", data={
            "email": email,
            "password": password,
        }, follow_redirects=False)

    def future_dates(self, start_offset=3, days=2):
        pickup = date.today() + timedelta(days=start_offset)
        ret = pickup + timedelta(days=days)
        return pickup.isoformat(), ret.isoformat()

    def test_demo_accounts_are_seeded_with_customer_role(self):
        db = self.logic.get_db()
        users = {
            row["email"]: row["role"]
            for row in db.execute("SELECT email, role FROM users").fetchall()
        }
        db.close()

        self.assertEqual(users["customer@driveflow.com"], "customer")
        self.assertEqual(users["staff@driveflow.com"], "staff")
        self.assertEqual(users["admin@driveflow.com"], "admin")

    def test_customer_can_book_and_pay_for_future_rental(self):
        self.login("customer@driveflow.com", "customer123")
        pickup, ret = self.future_dates()

        response = self.client.post("/book/1", data={
            "pickup_date": pickup,
            "return_date": ret,
            "promo_code": "",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/pay/", response.headers["Location"])
        booking_id = int(response.headers["Location"].rstrip("/").split("/")[-1])

        response = self.client.post(f"/pay/{booking_id}", data={
            "method": "Card",
            "card_number": "4242 4242 4242 4242",
            "expiry": "12/30",
            "cvv": "123",
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/receipt/{booking_id}", response.headers["Location"])

        db = self.logic.get_db()
        booking = db.execute("SELECT status FROM bookings WHERE id=?", (booking_id,)).fetchone()
        payment = db.execute("SELECT amount, status FROM payments WHERE booking_id=?", (booking_id,)).fetchone()
        vehicle = db.execute("SELECT status FROM vehicles WHERE id=1").fetchone()
        db.close()

        self.assertEqual(booking["status"], "Confirmed")
        self.assertEqual(payment["status"], "Paid")
        self.assertGreater(payment["amount"], 0)
        self.assertEqual(vehicle["status"], "Available")

    def test_staff_and_admin_are_not_allowed_to_rent_as_customers(self):
        self.login("staff@driveflow.com", "staff123")
        pickup, ret = self.future_dates(start_offset=8)
        response = self.client.post("/book/2", data={
            "pickup_date": pickup,
            "return_date": ret,
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/staff", response.headers["Location"])

        self.client.get("/logout")
        self.login("admin@driveflow.com", "admin123")
        response = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin", response.headers["Location"])

    def test_date_search_hides_conflicting_bookings(self):
        self.login("customer@driveflow.com", "customer123")
        pickup, ret = self.future_dates(start_offset=14)
        self.client.post("/book/3", data={
            "pickup_date": pickup,
            "return_date": ret,
        }, follow_redirects=False)

        response = self.client.get(f"/vehicles?pickup_date={pickup}&return_date={ret}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"2022 Honda Civic", response.data)

        later_pickup, later_ret = self.future_dates(start_offset=20)
        response = self.client.get(f"/vehicles?pickup_date={later_pickup}&return_date={later_ret}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2022 Honda Civic", response.data)

    def test_availability_calendar_route_renders(self):
        response = self.client.get("/availability")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Vehicle booking calendar", response.data)


if __name__ == "__main__":
    unittest.main()
