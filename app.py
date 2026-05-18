from flask import Flask, request, session, url_for
from config import config
from models import db
from datetime import datetime, timedelta

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

    @app.after_request
    def add_wallet_adapter_cors_headers(response):
        if request.path.startswith('/api/'):
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        return response

    @app.context_processor
    def inject_nav_notifications():
        if not session.get('user_id'):
            return {
                'nav_notification_count': 0,
                'nav_notifications': [],
                'current_user': None,
                'hellobar_items': [],
                'hellobar_count': 0,
                'hellobar_primary': None,
                'guest_recovery_notice': None,
            }

        from models import User, Order, Swap, P2PTrade, P2PTradeParticipantState

        user_id = session['user_id']
        current_user = User.query.get(user_id)
        if not current_user:
            return {
                'nav_notification_count': 0,
                'nav_notifications': [],
                'current_user': None,
                'hellobar_items': [],
                'hellobar_count': 0,
                'hellobar_primary': None,
                'guest_recovery_notice': None,
            }
        trades = P2PTrade.query.filter(
            (P2PTrade.creator_id == user_id) | (P2PTrade.counterparty_id == user_id)
        ).order_by(P2PTrade.updated_at.desc()).all()
        swaps = Swap.query.join(Order).filter(
            (Order.user_id == user_id) | (Swap.matcher_id == user_id)
        ).order_by(Swap.updated_at.desc()).all()

        states = P2PTradeParticipantState.query.filter_by(user_id=user_id).all()
        state_by_trade_id = {state.trade_id: state for state in states}

        notifications = []
        hellobar_items = []
        fallback_seen = datetime.min
        final_trade_statuses = {'completed', 'canceled', 'disputed', 'no_show'}
        final_swap_statuses = {'completed', 'refunded', 'canceled', 'disputed'}
        swap_step_labels = {
            'pending_secret': ('Alice', 'generate secret hash'),
            'initiated': ('Alice', 'post HNS lock'),
            'alice_locked': ('Bob', 'verify HNS and post BTC lock'),
            'bob_locked': ('Alice', 'verify BTC and claim BTC'),
            'alice_claimed': ('Bob', 'claim HNS'),
        }

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

            if trade.status not in final_trade_statuses:
                if trade.offer.side == 'sell':
                    current_role = 'Alice' if trade.creator_id == user_id else 'Bob'
                else:
                    current_role = 'Bob' if trade.creator_id == user_id else 'Alice'
                hellobar_items.append({
                    'kind': 'P2P',
                    'label': f'P2P Trade #{trade.id}',
                    'detail': f'{trade.status} / {trade.milestone} | You are {current_role}',
                    'href': url_for('main.p2p_trade_room', trade_id=trade.id),
                    'priority': 1,
                })

        for swap in swaps:
            if swap.status in final_swap_statuses:
                continue

            alice_id = swap.role_alice_user_id
            bob_id = swap.matcher_id if swap.order.user_id == alice_id else swap.order.user_id
            step_role, step_action = swap_step_labels.get(swap.status, ('Next party', 'continue swap'))
            next_actor_id = alice_id if step_role == 'Alice' else bob_id if step_role == 'Bob' else None
            next_actor = User.query.get(next_actor_id) if next_actor_id else None
            user_is_next = next_actor_id == user_id
            user_role = 'Alice' if alice_id == user_id else 'Bob'
            pending_since = swap.updated_at or swap.created_at or datetime.utcnow()
            reminder_due = datetime.utcnow() >= pending_since + timedelta(hours=18)
            action_prefix = 'Reminder due' if reminder_due and not user_is_next else 'Next action'
            hellobar_items.append({
                'kind': 'Atomic',
                'label': f'Atomic Swap #{swap.id}',
                'detail': f'{action_prefix}: {step_role} ({next_actor.username if next_actor else "Unknown"}) must {step_action} | You are {user_role} ({current_user.username})',
                'href': url_for('main.swap_details', id=swap.id),
                'priority': 0 if user_is_next else 2,
                'user_is_next': user_is_next,
            })

        hellobar_items.sort(key=lambda item: item['priority'])
        guest_recovery_notice = None
        if session.get('auth_method') == 'guest':
            guest_recovery_notice = {
                'title': f'Save your guest recovery key for {current_user.username}',
                'detail': 'Use it later to return as this same anonymous P2P identity, even from another browser.',
                'href': url_for('auth.guest_recovery'),
            }

        return {
            'current_user': current_user,
            'nav_notification_count': len(notifications),
            'nav_notifications': notifications[:5],
            'hellobar_items': hellobar_items[:3],
            'hellobar_count': len(hellobar_items),
            'hellobar_primary': hellobar_items[0] if hellobar_items else None,
            'guest_recovery_notice': guest_recovery_notice,
        }

    return app
