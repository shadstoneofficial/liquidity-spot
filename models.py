from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String(36), primary_key=True)          # UUID str from GFAVIP user_id
    username = db.Column(db.String(80), unique=True)
    email = db.Column(db.String(120), unique=True)
    tier = db.Column(db.String(20))                           # free / paid / team
    gems_balance = db.Column(db.Integer, default=0)
    completed_swaps = db.Column(db.Integer, default=0)       # reputation counter
    last_sync = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'))
    side = db.Column(db.String(10))                           # 'buy' or 'sell' HNS
    amount_hns = db.Column(db.Numeric(precision=18, scale=8))
    price_btc_per_hns = db.Column(db.Numeric(precision=18, scale=8))
    gems_stake = db.Column(db.Integer, nullable=True)        # optional skin-in-game stake
    status = db.Column(db.String(20), default='open')        # open / matched / canceled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', backref='orders')

class Swap(db.Model):
    __tablename__ = 'swaps'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'))
    matcher_id = db.Column(db.String(36), db.ForeignKey('users.id'))
    secret_hash = db.Column(db.String(64))                    # sha256 hex
    role_alice_user_id = db.Column(db.String(36))             # who locks first (HNS if poster sells, etc.)
    timelock_alice_sec = db.Column(db.Integer, default=172800)  # 48h
    timelock_bob_sec = db.Column(db.Integer, default=86400)     # 24h
    status = db.Column(db.String(20), default='initiated')    # pending_secret / initiated / canceled / completed
    gems_escrow = db.Column(db.Integer, default=0)            # staked amount held in limbo
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    order = db.relationship('Order', backref='swap')
    matcher = db.relationship('User', foreign_keys=[matcher_id])
