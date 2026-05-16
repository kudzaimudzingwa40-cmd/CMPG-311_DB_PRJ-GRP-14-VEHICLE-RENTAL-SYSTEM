# DriveFlow Rental

DriveFlow Rental is a digital vehicle rental system designed to make browsing, booking, payment, and fleet management simple. The platform gives customers a smooth car rental experience with clear pricing, vehicle categories, loyalty rewards, booking history, and secure checkout.

The system also includes administrative tools for managing vehicles, bookings, customers, promotions, maintenance records, penalties, reports, and audit logs.

## Features

- Vehicle search and category filtering
- Customer registration and login
- Booking creation with date validation
- Promo code and loyalty points support
- Payment confirmation and receipts
- Customer dashboard for trips and penalties
- Admin dashboard for fleet and booking operations
- Staff tools for vehicle status and maintenance updates
- Raw SQL database operations using SQLite

## Running the App

```bash
python logic.py
```

Then open:

```text
http://127.0.0.1:5000/
```

## Demo Accounts

```text
Customer: customer@driveflow.com / customer123
Admin: admin@driveflow.com / admin123
Staff: staff@driveflow.com / staff123
```

## Verified Rental Flow

Customers can register or sign in with the customer demo account, search for date-based availability, reserve a vehicle, apply promo codes or loyalty points, and complete payment. Admin and staff accounts are role-gated for operations dashboards only, while customer rental actions are restricted to customer accounts.

```bash
python -m unittest discover -s tests
```
