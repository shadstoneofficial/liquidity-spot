import os
from app import create_app
from models import db
from sqlalchemy import inspect, text

# Create app instance
app = create_app(os.getenv('FLASK_ENV', 'default'))

# Verify config loading
if not app.config:
    print("WARNING: App config is empty!", flush=True)

def ensure_user_schema():
    """Lightweight schema patching for account preferences and reputation."""
    inspector = inspect(db.engine)

    if 'users' not in inspector.get_table_names():
        return

    existing_columns = {col['name'] for col in inspector.get_columns('users')}
    required_columns = {
        'stale_cancellations': "INTEGER DEFAULT 0",
        'stale_no_shows': "INTEGER DEFAULT 0",
        'disputed_swaps': "INTEGER DEFAULT 0",
        'guest_recovery_digest': "VARCHAR(64)",
        'notify_email': "BOOLEAN DEFAULT FALSE",
        'notify_telegram': "BOOLEAN DEFAULT FALSE",
        'notify_wallet': "BOOLEAN DEFAULT TRUE",
        'telegram_handle': "VARCHAR(80)",
    }

    with db.engine.begin() as connection:
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                print(f"Adding missing column users.{column_name}...", flush=True)
                connection.execute(
                    text(f"ALTER TABLE users ADD COLUMN {column_name} {column_type}")
                )

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

def ensure_p2p_feedback_schema():
    """Create feedback indexes for completed P2P trade reputation."""
    inspector = inspect(db.engine)

    if 'p2p_trade_feedback' not in inspector.get_table_names():
        return

    index_names = {index['name'] for index in inspector.get_indexes('p2p_trade_feedback')}
    with db.engine.begin() as connection:
        if 'idx_p2p_trade_feedback_reviewee' not in index_names:
            connection.execute(text(
                "CREATE INDEX idx_p2p_trade_feedback_reviewee ON p2p_trade_feedback (reviewee_id)"
            ))
        if 'idx_p2p_trade_feedback_trade' not in index_names:
            connection.execute(text(
                "CREATE INDEX idx_p2p_trade_feedback_trade ON p2p_trade_feedback (trade_id)"
            ))

def ensure_atomic_swap_schema():
    """Lightweight schema patching for the manual HTLC lifecycle."""
    inspector = inspect(db.engine)

    if 'swaps' not in inspector.get_table_names():
        return

    existing_columns = {col['name'] for col in inspector.get_columns('swaps')}
    required_columns = {
        'alice_lock_txid': "VARCHAR(128)",
        'bob_lock_txid': "VARCHAR(128)",
        'alice_claim_txid': "VARCHAR(128)",
        'bob_claim_txid': "VARCHAR(128)",
        'alice_refund_txid': "VARCHAR(128)",
        'bob_refund_txid': "VARCHAR(128)",
        'revealed_secret': "VARCHAR(128)",
        'alice_lock_verified_at': "TIMESTAMP",
        'bob_lock_verified_at': "TIMESTAMP",
        'alice_claim_verified_at': "TIMESTAMP",
        'bob_claim_verified_at': "TIMESTAMP",
        'adapter_error': "TEXT",
        'adapter_token': "VARCHAR(64)",
        'hns_claim_public_key': "VARCHAR(130)",
        'hns_refund_public_key': "VARCHAR(130)",
        'hns_refund_locktime': "INTEGER",
        'hns_lock_address': "VARCHAR(128)",
        'hns_lock_script': "TEXT",
        'hns_lock_value': "INTEGER",
        'hns_lock_output_index': "INTEGER",
        'latest_note': "TEXT",
        'admin_review_status': "VARCHAR(20) DEFAULT 'unreviewed'",
        'admin_resolution': "VARCHAR(30)",
        'admin_notes': "TEXT",
        'last_reminder_at': "TIMESTAMP",
        'updated_at': "TIMESTAMP",
        'completed_at': "TIMESTAMP",
    }

    with db.engine.begin() as connection:
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                print(f"Adding missing column swaps.{column_name}...", flush=True)
                connection.execute(
                    text(f"ALTER TABLE swaps ADD COLUMN {column_name} {column_type}")
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
        ensure_user_schema()
        ensure_p2p_schema()
        ensure_p2p_feedback_schema()
        ensure_atomic_swap_schema()
        ensure_numeric_precision()
        print("Database tables created!", flush=True)
except Exception as e:
    print(f"Error initializing database: {e}", flush=True)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
