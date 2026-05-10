from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, Response
from models import db, User, Order, Swap, P2POffer, P2PTrade, P2PTradeMessage, P2PTradeParticipantState
import os
from routes.auth import login_required
import secrets
import hashlib
import requests
from decimal import Decimal, InvalidOperation
from datetime import datetime
from services.gems_service import (
    GemsServiceError,
    credit_gems as wallet_credit_gems,
    deduct_gems as wallet_deduct_gems,
    is_wallet_service_configured,
)

main_bp = Blueprint('main', __name__)


def _is_gfavip_session():
    return bool(session.get('token')) or session.get('auth_method') == 'gfavip'


def _ensure_session_user():
    if session.get('user_id'):
        user = User.query.get(session['user_id'])
        if user:
            return user

    for _ in range(5):
        user_id = f"guest-{secrets.token_hex(16)}"
        username = f"Guest {secrets.token_hex(3)}"
        if not User.query.get(user_id) and not User.query.filter_by(username=username).first():
            user = User(id=user_id, username=username, tier='guest')
            db.session.add(user)
            db.session.commit()
            session['user_id'] = user_id
            session['username'] = username
            session['tier'] = 'guest'
            session['auth_method'] = 'guest'
            flash('Continuing as a guest. GFAVIP is only needed for Gems and account-linked benefits.', 'info')
            return user

    raise RuntimeError('Could not create a guest session.')


def _parse_gems_stake(raw_value):
    try:
        return int(raw_value or 0)
    except (TypeError, ValueError):
        return None


def _gems_allowed_or_flash(gems_stake):
    if gems_stake and gems_stake > 0 and not _is_gfavip_session():
        flash('GFAVIP login is only required when you choose to use Gems. Remove the Gems bond or sign in with GFAVIP.', 'warning')
        return False
    return True


def _build_gems_metadata(trade, operation_type, extra=None):
    metadata = {
        'service': 'liquidity-spot',
        'trade_id': trade.id,
        'offer_id': trade.offer_id,
        'operation_type': operation_type,
    }
    if extra:
        metadata.update(extra)
    return metadata


def _refund_maker_bond(trade, reason, resolution='refunded'):
    if trade.maker_bond_status != 'locked' or trade.maker_bond_amount <= 0:
        return False, 'No locked maker bond to refund.'

    try:
        wallet_credit_gems(
            trade.creator_id,
            trade.maker_bond_amount,
            reason,
            metadata=_build_gems_metadata(trade, 'maker_bond_refund', {
                'bond_resolution': resolution
            })
        )
    except GemsServiceError as exc:
        trade.maker_bond_error = str(exc)
        return False, str(exc)

    trade.maker_bond_status = 'refunded'
    trade.maker_bond_released_at = datetime.utcnow()
    trade.maker_bond_resolution = resolution
    trade.maker_bond_error = None
    return True, None

@main_bp.route('/skill.md')
def skill_md():
    skill_path = os.path.join(current_app.root_path, 'skill.md')
    if os.path.exists(skill_path):
        with open(skill_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return Response(content, mimetype='text/plain')
    return Response("skill.md not found", status=404, mimetype='text/plain')

@main_bp.route('/tutorial')
def tutorial():
    return render_template('tutorial.html')

@main_bp.route('/p2p')
def p2p():
    offers = P2POffer.query.filter_by(status='open').order_by(P2POffer.created_at.desc()).all()
    my_trades = []
    try:
        response = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=handshake&vs_currencies=btc')
        if response.status_code == 200:
            current_price = response.json().get('handshake', {}).get('btc', 0.000000500000)
        else:
            current_price = 0.000000500000
    except Exception as e:
        print(f"CoinGecko API Error: {e}")
        current_price = 0.000000500000

    if session.get('user_id'):
        my_trades = P2PTrade.query.filter(
            (P2PTrade.creator_id == session['user_id']) |
            (P2PTrade.counterparty_id == session['user_id'])
        ).order_by(P2PTrade.updated_at.desc()).all()

    return render_template('p2p.html', offers=offers, my_trades=my_trades, current_price=current_price)

@main_bp.route('/p2p/offers', methods=['POST'])
def create_p2p_offer():
    user = _ensure_session_user()
    side = request.form.get('side')
    amount_hns = request.form.get('amount_hns')
    price = request.form.get('price')
    gems_stake = _parse_gems_stake(request.form.get('gems_stake'))
    payment_method = request.form.get('payment_method') or 'Manual Wallet Transfer'
    notes = request.form.get('notes')

    if gems_stake is None:
        flash('Gems bond must be a whole number.', 'error')
        return redirect(url_for('main.p2p'))

    if not _gems_allowed_or_flash(gems_stake):
        return redirect(url_for('main.p2p'))

    try:
        offer = P2POffer(
            creator_id=user.id,
            side=side,
            amount_hns=Decimal(amount_hns),
            price_btc_per_hns=Decimal(price),
            gems_stake=gems_stake,
            payment_method=payment_method,
            notes=notes
        )
        db.session.add(offer)
        db.session.commit()
        flash('P2P offer created successfully.', 'success')
    except (InvalidOperation, ValueError) as exc:
        flash(f'Error creating P2P offer: {exc}', 'error')

    return redirect(url_for('main.p2p'))

@main_bp.route('/p2p/offers/<int:offer_id>/accept', methods=['POST'])
def accept_p2p_offer(offer_id):
    user = _ensure_session_user()
    offer = P2POffer.query.get_or_404(offer_id)

    if offer.creator_id == user.id:
        flash('You cannot accept your own P2P offer.', 'error')
        return redirect(url_for('main.p2p'))

    if offer.status != 'open':
        flash('This P2P offer is no longer available.', 'error')
        return redirect(url_for('main.p2p'))

    bond_status = 'none'
    bond_locked_at = None
    bond_error = None

    if offer.gems_stake and offer.gems_stake > 0:
        if not is_wallet_service_configured():
            flash('This offer includes a Gems bond, but the wallet service is not configured yet.', 'error')
            return redirect(url_for('main.p2p'))

        try:
            wallet_deduct_gems(
                offer.creator_id,
                offer.gems_stake,
                f'Liquidity.spot maker bond locked for P2P offer #{offer.id}',
                metadata={
                    'service': 'liquidity-spot',
                    'offer_id': offer.id,
                    'operation_type': 'maker_bond_lock'
                }
            )
            bond_status = 'locked'
            bond_locked_at = datetime.utcnow()
        except GemsServiceError as exc:
            bond_status = 'failed'
            bond_error = str(exc)
            flash(f'Could not lock the maker Gems bond: {exc}', 'error')
            return redirect(url_for('main.p2p'))

    trade = P2PTrade(
        offer_id=offer.id,
        creator_id=offer.creator_id,
        counterparty_id=user.id,
        status='matched',
        milestone='matched',
        latest_note='Trade matched. Use this room to coordinate next steps safely.',
        last_actor_user_id=user.id,
        maker_bond_amount=offer.gems_stake or 0,
        maker_bond_status=bond_status,
        maker_bond_locked_at=bond_locked_at,
        maker_bond_error=bond_error
    )
    offer.status = 'matched'

    db.session.add(trade)
    db.session.commit()

    db.session.add(P2PTradeParticipantState(
        trade_id=trade.id,
        user_id=user.id,
        last_viewed_at=datetime.utcnow()
    ))
    db.session.commit()

    flash('P2P trade room created.', 'success')
    return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))

@main_bp.route('/p2p/trades/<int:trade_id>')
@login_required
def p2p_trade_room(trade_id):
    trade = P2PTrade.query.get_or_404(trade_id)

    if session['user_id'] not in [trade.creator_id, trade.counterparty_id]:
        flash('You do not have permission to view this trade.', 'error')
        return redirect(url_for('main.p2p'))

    is_creator = session['user_id'] == trade.creator_id
    if trade.offer.side == 'sell':
        alice_user = trade.creator
        bob_user = trade.counterparty
    else:
        alice_user = trade.counterparty
        bob_user = trade.creator

    current_role = 'Alice' if session['user_id'] == alice_user.id else 'Bob'

    participant_state = P2PTradeParticipantState.query.filter_by(
        trade_id=trade.id,
        user_id=session['user_id']
    ).first()
    if participant_state:
        participant_state.last_viewed_at = datetime.utcnow()
    else:
        db.session.add(P2PTradeParticipantState(
            trade_id=trade.id,
            user_id=session['user_id'],
            last_viewed_at=datetime.utcnow()
        ))
    db.session.commit()

    return render_template(
        'p2p_trade_room.html',
        trade=trade,
        is_creator=is_creator,
        alice_user=alice_user,
        bob_user=bob_user,
        current_role=current_role
    )

@main_bp.route('/p2p/trades/<int:trade_id>/update', methods=['POST'])
@login_required
def update_p2p_trade(trade_id):
    trade = P2PTrade.query.get_or_404(trade_id)

    if session['user_id'] not in [trade.creator_id, trade.counterparty_id]:
        flash('You do not have permission to update this trade.', 'error')
        return redirect(url_for('main.p2p'))

    milestone = request.form.get('milestone')
    status = request.form.get('status')
    alice_lock_txid = request.form.get('alice_lock_txid')
    bob_lock_txid = request.form.get('bob_lock_txid')
    latest_note = request.form.get('latest_note')

    if milestone:
        trade.milestone = milestone
    if status:
        trade.status = status
    if alice_lock_txid:
        trade.alice_lock_txid = alice_lock_txid
    if bob_lock_txid:
        trade.bob_lock_txid = bob_lock_txid
    if latest_note:
        trade.latest_note = latest_note

    trade.last_actor_user_id = session['user_id']

    db.session.commit()
    flash('P2P trade updated.', 'success')
    return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))

@main_bp.route('/p2p/trades/<int:trade_id>/message', methods=['POST'])
@login_required
def add_p2p_trade_message(trade_id):
    trade = P2PTrade.query.get_or_404(trade_id)

    if session['user_id'] not in [trade.creator_id, trade.counterparty_id]:
        flash('You do not have permission to post in this trade.', 'error')
        return redirect(url_for('main.p2p'))

    message = (request.form.get('message') or '').strip()
    if not message:
        flash('Message cannot be empty.', 'error')
        return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))

    db.session.add(P2PTradeMessage(
        trade_id=trade.id,
        user_id=session['user_id'],
        message=message
    ))
    trade.last_actor_user_id = session['user_id']
    db.session.commit()

    flash('Message added.', 'success')
    return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))

@main_bp.route('/p2p/trades/<int:trade_id>/action', methods=['POST'])
@login_required
def p2p_trade_action(trade_id):
    trade = P2PTrade.query.get_or_404(trade_id)

    if session['user_id'] not in [trade.creator_id, trade.counterparty_id]:
        flash('You do not have permission to update this trade.', 'error')
        return redirect(url_for('main.p2p'))

    action = request.form.get('action')
    note = (request.form.get('note') or '').strip()

    action_map = {
        'mark_payment_sent': ('matched', 'payment_sent', 'Payment or lock has been sent.'),
        'mark_payment_received': ('matched', 'payment_received', 'Payment or lock has been confirmed.'),
        'mark_released': ('matched', 'released', 'Funds released or claim step completed.'),
        'mark_completed': ('completed', 'completed', 'Trade marked completed by a participant.'),
        'mark_disputed': ('disputed', trade.milestone, 'Trade was marked disputed by a participant.'),
        'mark_no_show': ('no_show', trade.milestone, 'Counterparty was marked as no-show.'),
        'mark_canceled': ('canceled', trade.milestone, 'Trade was canceled by a participant.')
    }

    if action not in action_map:
        flash('Unknown P2P action.', 'error')
        return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))

    status, milestone, default_note = action_map[action]
    trade.status = status
    trade.milestone = milestone
    trade.latest_note = note or default_note
    trade.last_actor_user_id = session['user_id']

    if status in ['disputed', 'no_show', 'canceled']:
        trade.admin_review_status = 'in_review'

    if note:
        db.session.add(P2PTradeMessage(
            trade_id=trade.id,
            user_id=session['user_id'],
            message=f'[{action}] {note}'
        ))

    if action == 'mark_completed':
        refunded, refund_error = _refund_maker_bond(
            trade,
            reason=f'Liquidity.spot maker bond refund for completed P2P trade #{trade.id}'
        )
        if refunded:
            flash('Maker Gems bond refunded on completion.', 'success')
        elif trade.maker_bond_amount > 0 and trade.maker_bond_status == 'locked':
            flash(f'Trade completed, but maker bond refund still needs attention: {refund_error}', 'error')

    db.session.commit()
    flash('Trade action recorded.', 'success')
    return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))

@main_bp.route('/p2p/offers/<int:offer_id>/cancel', methods=['POST'])
@login_required
def cancel_p2p_offer(offer_id):
    offer = P2POffer.query.get_or_404(offer_id)

    if offer.creator_id != session['user_id']:
        flash('You can only cancel your own P2P offers.', 'error')
        return redirect(url_for('main.p2p'))

    if offer.status != 'open':
        flash('This P2P offer can no longer be canceled.', 'error')
        return redirect(url_for('main.p2p'))

    offer.status = 'canceled'
    db.session.commit()
    flash('P2P offer canceled.', 'success')
    return redirect(url_for('main.p2p'))

@main_bp.route('/')
def index():
    return render_template('index.html')

@main_bp.route('/dashboard')
@login_required
def dashboard():
    user = User.query.get(session['user_id'])
    my_orders = Order.query.filter_by(user_id=user.id).all()
    # Filter swaps where user is either the order creator or the matcher
    my_swaps = Swap.query.join(Order).filter(
        (Order.user_id == user.id) | (Swap.matcher_id == user.id)
    ).all()
    my_p2p_trades = P2PTrade.query.filter(
        (P2PTrade.creator_id == user.id) | (P2PTrade.counterparty_id == user.id)
    ).order_by(P2PTrade.updated_at.desc()).all()

    active_swaps = [swap for swap in my_swaps if swap.status not in ['completed', 'canceled']]
    active_p2p_trades = [trade for trade in my_p2p_trades if trade.status not in ['completed', 'canceled']]

    return render_template(
        'dashboard.html',
        user=user,
        orders=my_orders,
        swaps=my_swaps,
        active_swaps=active_swaps,
        active_p2p_trades=active_p2p_trades
    )


@main_bp.route('/profile')
@login_required
def profile():
    user = User.query.get_or_404(session['user_id'])
    p2p_offer_count = P2POffer.query.filter_by(creator_id=user.id).count()
    p2p_trade_count = P2PTrade.query.filter(
        (P2PTrade.creator_id == user.id) | (P2PTrade.counterparty_id == user.id)
    ).count()
    order_count = Order.query.filter_by(user_id=user.id).count()

    return render_template(
        'profile.html',
        user=user,
        p2p_offer_count=p2p_offer_count,
        p2p_trade_count=p2p_trade_count,
        order_count=order_count
    )


@main_bp.route('/activity')
@login_required
def activity():
    user = User.query.get_or_404(session['user_id'])
    items = []

    offers = P2POffer.query.filter_by(creator_id=user.id).all()
    for offer in offers:
        items.append({
            'when': offer.created_at,
            'category': 'P2P Offer',
            'title': f'Created P2P offer #{offer.id}',
            'detail': f'{offer.side.upper()} {offer.amount_hns} HNS at {offer.price_btc_per_hns} BTC/HNS with {offer.gems_stake} Gems bond',
            'href': url_for('main.p2p')
        })

    trades = P2PTrade.query.filter(
        (P2PTrade.creator_id == user.id) | (P2PTrade.counterparty_id == user.id)
    ).all()
    for trade in trades:
        items.append({
            'when': trade.updated_at or trade.created_at,
            'category': 'P2P Trade',
            'title': f'Updated P2P trade #{trade.id}',
            'detail': f'Status: {trade.status} | Milestone: {trade.milestone}',
            'href': url_for('main.p2p_trade_room', trade_id=trade.id)
        })
        if trade.maker_bond_amount and trade.creator_id == user.id:
            items.append({
                'when': trade.maker_bond_released_at or trade.maker_bond_locked_at or trade.updated_at or trade.created_at,
                'category': 'Gems Bond',
                'title': f'Maker bond {trade.maker_bond_status} for trade #{trade.id}',
                'detail': f'{trade.maker_bond_amount} Gems | Resolution: {trade.maker_bond_resolution or "pending"}',
                'href': url_for('main.p2p_trade_room', trade_id=trade.id)
            })

    messages = P2PTradeMessage.query.filter_by(user_id=user.id).all()
    for message in messages:
        items.append({
            'when': message.created_at,
            'category': 'Message',
            'title': f'Posted message in trade #{message.trade_id}',
            'detail': message.message[:120],
            'href': url_for('main.p2p_trade_room', trade_id=message.trade_id)
        })

    orders = Order.query.filter_by(user_id=user.id).all()
    for order in orders:
        items.append({
            'when': order.created_at,
            'category': 'Atomic Swap Order',
            'title': f'Created atomic swap order #{order.id}',
            'detail': f'{order.side.upper()} {order.amount_hns} HNS at {order.price_btc_per_hns} BTC/HNS',
            'href': url_for('main.orders')
        })

    swaps = Swap.query.join(Order).filter(
        (Order.user_id == user.id) | (Swap.matcher_id == user.id)
    ).all()
    for swap in swaps:
        items.append({
            'when': swap.created_at,
            'category': 'Atomic Swap',
            'title': f'Active atomic swap #{swap.id}',
            'detail': f'Status: {swap.status}',
            'href': url_for('main.swap_details', id=swap.id)
        })

    items.sort(key=lambda item: item['when'] or datetime.min, reverse=True)

    return render_template('activity.html', user=user, items=items)

@main_bp.route('/orders', methods=['GET', 'POST'])
def orders():
    if request.method == 'POST':
        user = _ensure_session_user()
        side = request.form.get('side')
        amount_hns = request.form.get('amount_hns')
        price = request.form.get('price')
        gems_stake = _parse_gems_stake(request.form.get('gems_stake'))

        if gems_stake is None:
            flash('Gems stake must be a whole number.', 'error')
            return redirect(url_for('main.orders'))

        if not _gems_allowed_or_flash(gems_stake):
            return redirect(url_for('main.orders'))
        
        try:
            order = Order(
                user_id=user.id,
                side=side,
                amount_hns=Decimal(amount_hns),
                price_btc_per_hns=Decimal(price),
                gems_stake=gems_stake
            )
            db.session.add(order)
            db.session.commit()
            flash('Order created successfully!', 'success')
            return redirect(url_for('main.orders'))
        except (InvalidOperation, ValueError) as e:
            flash(f'Error creating order: {str(e)}', 'error')

    orders = Order.query.filter_by(status='open').order_by(Order.created_at.desc()).all()
    
    # Get current HNS price in BTC
    try:
        response = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=handshake&vs_currencies=btc')
        if response.status_code == 200:
            current_price = response.json().get('handshake', {}).get('btc', 0.000000500000)
        else:
            current_price = 0.000000500000
    except Exception as e:
        print(f"CoinGecko API Error: {e}")
        current_price = 0.000000500000
        
    return render_template('orders.html', orders=orders, current_price=current_price)

@main_bp.route('/orders/<int:order_id>/accept', methods=['POST'])
def accept_order(order_id):
    user = _ensure_session_user()
    order = Order.query.get_or_404(order_id)
    matcher_id = user.id
    
    if order.user_id == matcher_id:
        flash('You cannot accept your own order.', 'error')
        return redirect(url_for('main.orders'))
        
    if order.status != 'open':
        flash('Order is no longer available.', 'error')
        return redirect(url_for('main.orders'))

    # Determine roles
    # Case A: Order is "Sell HNS" (Maker wants to Sell HNS)
    #   - Maker = Alice (HNS Seller)
    #   - Taker/Matcher = Bob (HNS Buyer)
    #
    # Case B: Order is "Buy HNS" (Maker wants to Buy HNS)
    #   - Maker = Bob (HNS Buyer)
    #   - Taker/Matcher = Alice (HNS Seller)
    
    if order.side == 'sell':
        # Case A: Maker is Alice. Matcher is Bob.
        role_alice_id = order.user_id
        # Alice (Maker) needs to generate secret later
        status = 'pending_secret'
        secret = None
        secret_hash = None
    else:
        role_alice_id = matcher_id
        # Alice (Matcher) generates secret NOW
        status = 'initiated'
        secret = secrets.token_hex(32)
        secret_hash = hashlib.sha256(bytes.fromhex(secret)).hexdigest()
        session['generated_secret'] = secret

    swap = Swap(
        order_id=order.id,
        matcher_id=matcher_id,
        secret_hash=secret_hash,
        role_alice_user_id=role_alice_id,
        status=status
    )
    
    order.status = 'matched'
    db.session.add(swap)
    db.session.commit()
    
    if status == 'initiated':
        flash('Swap initiated! You are Alice. Save your secret!', 'success')
    else:
        flash('Swap matched! Waiting for Alice (Maker) to generate secret.', 'info')
        
    return redirect(url_for('main.swap_details', id=swap.id))

@main_bp.route('/swaps/<int:id>/initiate', methods=['POST'])
@login_required
def initiate_swap(id):
    swap = Swap.query.get_or_404(id)
    
    if swap.status != 'pending_secret':
        flash('Swap already initiated.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))
        
    if session['user_id'] != swap.role_alice_user_id:
        flash('Only Alice can initiate the swap.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))
        
    # Alice generates secret
    secret = secrets.token_hex(32)
    secret_hash = hashlib.sha256(bytes.fromhex(secret)).hexdigest()
    
    swap.secret_hash = secret_hash
    swap.status = 'initiated'
    db.session.commit()
    
    session['generated_secret'] = secret
    flash('Secret generated! Swap is now live.', 'success')
    return redirect(url_for('main.swap_details', id=swap.id))

@main_bp.route('/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
def cancel_order(order_id):
    order = Order.query.get_or_404(order_id)
    
    if order.user_id != session['user_id']:
        flash('You can only cancel your own orders.', 'error')
        return redirect(url_for('main.dashboard'))
        
    if order.status != 'open':
        flash('Cannot cancel order. It may have already been matched or canceled.', 'error')
        return redirect(url_for('main.dashboard'))
        
    order.status = 'canceled'
    db.session.commit()
    
    flash('Order canceled successfully.', 'success')
    return redirect(url_for('main.dashboard'))

@main_bp.route('/swaps/<int:id>')
@login_required
def swap_details(id):
    swap = Swap.query.get_or_404(id)
    user = User.query.get(session['user_id'])
    
    # Check if user is involved
    if user.id != swap.order.user_id and user.id != swap.matcher_id:
        flash('You do not have permission to view this swap.', 'error')
        return redirect(url_for('main.dashboard'))
        
    secret = session.get('generated_secret') if swap.matcher_id == user.id else None
    
    # Get coingecko price (mock for now or real request)
    try:
        # response = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=handshake&vs_currencies=btc')
        # price = response.json()['handshake']['btc']
        price = 0.000000500000 # Placeholder
    except:
        price = 0.00000000
        
    return render_template('swap.html', swap=swap, user=user, secret=secret, coingecko_price=price)
