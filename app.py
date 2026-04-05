from flask import Flask, session
from config import config
from models import db
from datetime import datetime

def create_app(config_name='default'):
    app = Flask(__name__)
    
    # Ensure config_name is valid
    if config_name not in config:
        print(f"Warning: Config '{config_name}' not found. Using 'default'.", flush=True)
        config_name = 'default'
        
    app.config.from_object(config[config_name])

    db.init_app(app)

    from routes.auth import auth_bp
    app.register_blueprint(auth_bp)

    from routes.main import main_bp
    app.register_blueprint(main_bp)

    from routes.admin import admin_bp
    app.register_blueprint(admin_bp)

    @app.context_processor
    def inject_nav_notifications():
        if not session.get('user_id'):
            return {
                'nav_notification_count': 0,
                'nav_notifications': [],
                'current_user': None
            }

        from models import User, P2PTrade, P2PTradeParticipantState

        user_id = session['user_id']
        current_user = User.query.get(user_id)
        trades = P2PTrade.query.filter(
            (P2PTrade.creator_id == user_id) | (P2PTrade.counterparty_id == user_id)
        ).order_by(P2PTrade.updated_at.desc()).all()

        states = P2PTradeParticipantState.query.filter_by(user_id=user_id).all()
        state_by_trade_id = {state.trade_id: state for state in states}

        notifications = []
        fallback_seen = datetime.min

        for trade in trades:
            state = state_by_trade_id.get(trade.id)
            last_seen_at = state.last_viewed_at if state else fallback_seen

            latest_message = None
            if trade.messages:
                latest_message = max(trade.messages, key=lambda message: message.created_at)

            reason = None
            if latest_message and latest_message.user_id != user_id and latest_message.created_at > last_seen_at:
                reason = 'New message'
            elif trade.updated_at and trade.updated_at > last_seen_at and trade.last_actor_user_id and trade.last_actor_user_id != user_id:
                reason = 'Trade update'

            if reason:
                notifications.append({
                    'trade_id': trade.id,
                    'reason': reason,
                    'status': trade.status,
                    'milestone': trade.milestone
                })

        return {
            'current_user': current_user,
            'nav_notification_count': len(notifications),
            'nav_notifications': notifications[:5]
        }

    return app
