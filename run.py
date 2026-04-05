import os
from app import create_app
from models import db
from sqlalchemy import inspect, text

# Create app instance
app = create_app(os.getenv('FLASK_ENV', 'default'))

# Verify config loading
if not app.config:
    print("WARNING: App config is empty!", flush=True)

def ensure_p2p_schema():
    """Lightweight schema patching for environments without migrations."""
    inspector = inspect(db.engine)

    if 'p2p_trades' not in inspector.get_table_names():
        return

    existing_columns = {col['name'] for col in inspector.get_columns('p2p_trades')}
    required_columns = {
        'admin_review_status': "VARCHAR(20) DEFAULT 'unreviewed'",
        'admin_resolution': "VARCHAR(30)",
        'admin_notes': "TEXT",
        'last_actor_user_id': "VARCHAR(36)",
        'maker_bond_amount': "INTEGER DEFAULT 0",
        'maker_bond_status': "VARCHAR(20) DEFAULT 'none'",
        'maker_bond_locked_at': "TIMESTAMP",
        'maker_bond_released_at': "TIMESTAMP",
        'maker_bond_resolution': "VARCHAR(30)",
        'maker_bond_error': "TEXT",
    }

    with db.engine.begin() as connection:
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                print(f"Adding missing column p2p_trades.{column_name}...", flush=True)
                connection.execute(
                    text(f"ALTER TABLE p2p_trades ADD COLUMN {column_name} {column_type}")
                )

def ensure_numeric_precision():
    """Increase numeric precision for BTC pricing on PostgreSQL deployments."""
    if db.engine.dialect.name != 'postgresql':
        return

    with db.engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE orders ALTER COLUMN amount_hns TYPE NUMERIC(24,8)"
        ))
        connection.execute(text(
            "ALTER TABLE orders ALTER COLUMN price_btc_per_hns TYPE NUMERIC(24,12)"
        ))
        connection.execute(text(
            "ALTER TABLE p2p_offers ALTER COLUMN amount_hns TYPE NUMERIC(24,8)"
        ))
        connection.execute(text(
            "ALTER TABLE p2p_offers ALTER COLUMN price_btc_per_hns TYPE NUMERIC(24,12)"
        ))

# Run migrations/create tables on startup
# This is safe to run on every deploy for simple apps
try:
    with app.app_context():
        print("Creating/Verifying database tables...", flush=True)
        db.create_all()
        ensure_p2p_schema()
        ensure_numeric_precision()
        print("Database tables created!", flush=True)
except Exception as e:
    print(f"Error initializing database: {e}", flush=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
