# CHANGE 'main' to whatever your main Python file is named (without the .py)
# For example, if your file is 'system.py', change this to: from system import app, db
from main import app, db 
from sqlalchemy import text 

with app.app_context():
    # 1. Delete the old tables causing the error
    db.session.execute(text('DROP TABLE IF EXISTS infusions CASCADE;'))
    db.session.execute(text('DROP TABLE IF EXISTS reservations CASCADE;'))
    db.session.commit()
    
    # 2. Recreate them with the new pickup_time column
    db.create_all()
    
    print("✅ Database successfully reset! The missing column has been added.")
    print("You can now run your main python file.")