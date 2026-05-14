from flask import render_template, g
# Import get_db from your main app file
from app import get_db 

@app.route('/availability')
def availability():
    # 1. Get the database connection
    db = get_db()

    # 2. Execute the Raw SQL query
    # We use '?' for placeholders in SQLite if needed
    query = """
        SELECT 
            v.name, 
            v.status, 
            MAX(b.end_date) AS available_until
        FROM vehicles v
        LEFT JOIN bookings b ON v.id = b.vehicle_id
        GROUP BY v.id
    """
    
    # 3. Fetch all results
    vehicles = db.execute(query).fetchall()

    # 4. Render the template with the data
    return render_template(
        'availability_calendar.html', 
        vehicles=vehicles
    )