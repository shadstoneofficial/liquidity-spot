from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from models import db, User, Order, Swap, P2POffer, P2PTrade, P2PTradeMessage
from routes.auth import login_required
import secrets
import hashlib
import requests

main_bp = Blueprint('main', __name__)

@main_bp.route('/tutorial')
def tutorial():
    return render_template('tutorial.html')

@main_bp.route('/p2p')
def p2p():
    offers = P2POffer.query.filter_by(status='open').order_by(P2POffer.created_at.desc()).all()
    my_trades = []

    if session.get('user_id'):
        my_trades = P2PTrade.query.filter(
            (P2PTrade.creator_id == session['user_id']) |
            (P2PTrade.counterparty_id == session['user_id'])
        ).order_by(P2PTrade.updated_at.desc()).all()

    return render_template('p2p.html', offers=offers, my_trades=my_trades)

@main_bp.route('/p2p/offers', methods=['POST'])
@login_required
def create_p2p_offer():
    side = request.form.get('side')
    amount_hns = request.form.get('amount_hns')
    price = request.form.get('price')
    gems_stake = request.form.get('gems_stake') or 0
    payment_method = request.form.get('payment_method') or 'Manual Wallet Transfer'
    notes = request.form.get('notes')

    try:
        offer = P2POffer(
            creator_id=session['user_id'],
            side=side,
            amount_hns=float(amount_hns),
            price_btc_per_hns=float(price),
            gems_stake=int(gems_stake),
            payment_method=payment_method,
            notes=notes
        )
        db.session.add(offer)
        db.session.commit()
        flash('P2P offer created successfully.', 'success')
    except Exception as exc:
        flash(f'Error creating P2P offer: {exc}', 'error')

    return redirect(url_for('main.p2p'))

@main_bp.route('/p2p/offers/<int:offer_id>/accept', methods=['POST'])
@login_required
def accept_p2p_offer(offer_id):
    offer = P2POffer.query.get_or_404(offer_id)

    if offer.creator_id == session['user_id']:
        flash('You cannot accept your own P2P offer.', 'error')
        return redirect(url_for('main.p2p'))

    if offer.status != 'open':
        flash('This P2P offer is no longer available.', 'error')
        return redirect(url_for('main.p2p'))

    trade = P2PTrade(
        offer_id=offer.id,
        creator_id=offer.creator_id,
        counterparty_id=session['user_id'],
        status='matched',
        milestone='matched',
        latest_note='Trade matched. Use this room to coordinate next steps safely.'
    )
    offer.status = 'matched'

    db.session.add(trade)
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
    return render_template('p2p_trade_room.html', trade=trade, is_creator=is_creator)

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

    if status in ['disputed', 'no_show', 'canceled']:
        trade.admin_review_status = 'in_review'

    if note:
        db.session.add(P2PTradeMessage(
            trade_id=trade.id,
            user_id=session['user_id'],
            message=f'[{action}] {note}'
        ))

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
    if 'user_id' in session:
        return redirect(url_for('main.dashboard'))
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
    return render_template('dashboard.html', user=user, orders=my_orders, swaps=my_swaps)

@main_bp.route('/orders', methods=['GET', 'POST'])
@login_required
def orders():
    if request.method == 'POST':
        side = request.form.get('side')
        amount_hns = request.form.get('amount_hns')
        price = request.form.get('price')
        gems_stake = request.form.get('gems_stake') or 0
        
        try:
            order = Order(
                user_id=session['user_id'],
                side=side,
                amount_hns=float(amount_hns),
                price_btc_per_hns=float(price),
                gems_stake=int(gems_stake)
            )
            db.session.add(order)
            db.session.commit()
            flash('Order created successfully!', 'success')
            return redirect(url_for('main.orders'))
        except Exception as e:
            flash(f'Error creating order: {str(e)}', 'error')

    orders = Order.query.filter_by(status='open').order_by(Order.created_at.desc()).all()
    
    # Get current HNS price in BTC
    try:
        response = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=handshake&vs_currencies=btc')
        if response.status_code == 200:
            current_price = response.json().get('handshake', {}).get('btc', 0.00000050)
        else:
            current_price = 0.00000050
    except Exception as e:
        print(f"CoinGecko API Error: {e}")
        current_price = 0.00000050
        
    return render_template('orders.html', orders=orders, current_price=current_price)

@main_bp.route('/orders/<int:order_id>/accept', methods=['POST'])
@login_required
def accept_order(order_id):
    order = Order.query.get_or_404(order_id)
    matcher_id = session['user_id']
    
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
        price = 0.00000050 # Placeholder
    except:
        price = 0.00000000
        
    return render_template('swap.html', swap=swap, user=user, secret=secret, coingecko_price=price)
