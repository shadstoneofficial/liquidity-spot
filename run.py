import os
import sys
from app import create_app
from models import db
from sqlalchemy import text

# Create app instance
app = create_app(os.getenv('FLASK_ENV', 'default'))

# Verify config loading
if not app.config:
    print("WARNING: App config is empty!", flush=True)

# Run migrations/create tables on startup
# This is safe to run on every deploy for simple apps
try:
    with app.app_context():
        print("Creating/Verifying database tables...", flush=True)
        db.create_all()
        print("Database tables created!", flush=True)
except Exception as e:
    print(f"Error initializing database: {e}", flush=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
