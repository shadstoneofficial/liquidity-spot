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
    amount_hns = db.Column(db.Numeric(precision=24, scale=8))
    price_btc_per_hns = db.Column(db.Numeric(precision=24, scale=12))
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
    status = db.Column(db.String(30), default='initiated')    # pending_secret / initiated / alice_locked / bob_locked / alice_claimed / completed / canceled / refunded
    gems_escrow = db.Column(db.Integer, default=0)            # staked amount held in limbo
    alice_lock_txid = db.Column(db.String(128))
    bob_lock_txid = db.Column(db.String(128))
    alice_claim_txid = db.Column(db.String(128))
    bob_claim_txid = db.Column(db.String(128))
    alice_refund_txid = db.Column(db.String(128))
    bob_refund_txid = db.Column(db.String(128))
    revealed_secret = db.Column(db.String(128))
    alice_lock_verified_at = db.Column(db.DateTime)
    bob_lock_verified_at = db.Column(db.DateTime)
    alice_claim_verified_at = db.Column(db.DateTime)
    bob_claim_verified_at = db.Column(db.DateTime)
    adapter_error = db.Column(db.Text)
    adapter_token = db.Column(db.String(64))
    hns_claim_public_key = db.Column(db.String(130))
    hns_refund_public_key = db.Column(db.String(130))
    hns_refund_locktime = db.Column(db.Integer)
    hns_lock_address = db.Column(db.String(128))
    hns_lock_script = db.Column(db.Text)
    hns_lock_value = db.Column(db.Integer)
    hns_lock_output_index = db.Column(db.Integer)
    latest_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    order = db.relationship('Order', backref='swap')
    matcher = db.relationship('User', foreign_keys=[matcher_id])

class P2POffer(db.Model):
    __tablename__ = 'p2p_offers'
    id = db.Column(db.Integer, primary_key=True)
    creator_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    side = db.Column(db.String(10), nullable=False)            # buy or sell HNS
    amount_hns = db.Column(db.Numeric(precision=24, scale=8), nullable=False)
    price_btc_per_hns = db.Column(db.Numeric(precision=24, scale=12), nullable=False)
    gems_stake = db.Column(db.Integer, default=0)
    payment_method = db.Column(db.String(50), default='Manual Wallet Transfer')
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), default='open')          # open / matched / canceled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    creator = db.relationship('User', backref='p2p_offers')

class P2PTrade(db.Model):
    __tablename__ = 'p2p_trades'
    id = db.Column(db.Integer, primary_key=True)
    offer_id = db.Column(db.Integer, db.ForeignKey('p2p_offers.id'), nullable=False)
    creator_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    counterparty_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    status = db.Column(db.String(20), default='matched')       # matched / completed / canceled / disputed / no_show
    milestone = db.Column(db.String(30), default='matched')    # matched / payment_sent / payment_received / released / completed
    alice_lock_txid = db.Column(db.String(128))
    bob_lock_txid = db.Column(db.String(128))
    latest_note = db.Column(db.Text)
    admin_review_status = db.Column(db.String(20), default='unreviewed')   # unreviewed / in_review / resolved
    admin_resolution = db.Column(db.String(30))                            # completed / canceled / disputed / no_show / refunded
    admin_notes = db.Column(db.Text)
    last_actor_user_id = db.Column(db.String(36))
    maker_bond_amount = db.Column(db.Integer, default=0)
    maker_bond_status = db.Column(db.String(20), default='none')           # none / locked / refunded / slashed / failed
    maker_bond_locked_at = db.Column(db.DateTime)
    maker_bond_released_at = db.Column(db.DateTime)
    maker_bond_resolution = db.Column(db.String(30))                       # refunded / slashed_full / slashed_partial
    maker_bond_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    offer = db.relationship('P2POffer', backref='trade')
    creator = db.relationship('User', foreign_keys=[creator_id])
    counterparty = db.relationship('User', foreign_keys=[counterparty_id])

class P2PTradeMessage(db.Model):
    __tablename__ = 'p2p_trade_messages'
    id = db.Column(db.Integer, primary_key=True)
    trade_id = db.Column(db.Integer, db.ForeignKey('p2p_trades.id'), nullable=False)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    trade = db.relationship('P2PTrade', backref='messages')
    user = db.relationship('User')

class P2PTradeParticipantState(db.Model):
    __tablename__ = 'p2p_trade_participant_states'
    id = db.Column(db.Integer, primary_key=True)
    trade_id = db.Column(db.Integer, db.ForeignKey('p2p_trades.id'), nullable=False)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False)
    last_viewed_at = db.Column(db.DateTime, default=datetime.utcnow)
    trade = db.relationship('P2PTrade', backref='participant_states')
    user = db.relationship('User')
