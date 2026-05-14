from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, Response, jsonify
from models import db, User, Order, Swap, SwapMessage, P2POffer, P2PTrade, P2PTradeMessage, P2PTradeParticipantState
import os
from routes.auth import login_required
import secrets
import hashlib
import requests
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from services.gems_service import (
    GemsServiceError,
    credit_gems as wallet_credit_gems,
    deduct_gems as wallet_deduct_gems,
    is_wallet_service_configured,
)
from services.chain_watchers import WatcherError, verify_bitcoin_tx, verify_hns_tx
from services.swap_adapters import build_swap_intents

main_bp = Blueprint('main', __name__)
SWAP_STALE_CANCEL_HOURS = 24
SWAP_REMINDER_HOURS = 18
SWAP_REMINDER_REPEAT_HOURS = 6


def _is_gfavip_session():
    return bool(session.get('token')) or session.get('auth_method') == 'gfavip'


def _ensure_session_user():
    if session.get('user_id'):
        user = User.query.get(session['user_id'])
        if user:
            return user

    for _ in range(5):
        user_id = f"guest-{secrets.token_hex(15)}"
        username = f"Guest {secrets.token_hex(3)}"
        if not User.query.get(user_id) and not User.query.filter_by(username=username).first():
            user = User(id=user_id, username=username, tier='guest')
            db.session.add(user)
            try:
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
                continue
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


def _is_hex(value, expected_length=None):
    if not value:
        return False
    value = value.strip()
    if expected_length and len(value) != expected_length:
        return False
    return bool(re.fullmatch(r'[0-9a-fA-F]+', value))


def _clean_txid(raw_value):
    txid = (raw_value or '').strip()
    if not _is_hex(txid, 64):
        return None
    return txid.lower()


def _clean_secret(raw_value):
    secret = (raw_value or '').strip()
    if not _is_hex(secret):
        return None
    if len(secret) not in (32, 64, 128):
        return None
    return secret.lower()


def _clean_public_key(raw_value):
    key = (raw_value or '').strip()
    if len(key) not in (66, 130) or not _is_hex(key):
        return None
    return key.lower()


def _clean_script(raw_value):
    script = (raw_value or '').strip()
    if not script or len(script) > 4096 or not _is_hex(script):
        return None
    return script.lower()


def _clean_non_negative_int(raw_value):
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _hash_secret(secret):
    return hashlib.sha256(bytes.fromhex(secret)).hexdigest()


def _swap_participants(swap):
    return {swap.order.user_id, swap.matcher_id}


def _swap_alice_id(swap):
    return swap.role_alice_user_id


def _swap_bob_id(swap):
    for participant_id in _swap_participants(swap):
        if participant_id != swap.role_alice_user_id:
            return participant_id
    return None


def _is_swap_alice(swap, user_id):
    return user_id == _swap_alice_id(swap)


def _is_swap_bob(swap, user_id):
    return user_id == _swap_bob_id(swap)


def _swap_next_step(swap):
    alice_id = _swap_alice_id(swap)
    bob_id = _swap_bob_id(swap)
    steps = {
        'pending_secret': {
            'role': 'Alice',
            'user_id': alice_id,
            'title': 'Alice needs to generate the secret hash.',
            'instructions': [
                'Open this swap room and generate the secret hash.',
                'Do not lock HNS until the secret hash is visible in the room.',
                'Post a message if you need more time.'
            ],
        },
        'initiated': {
            'role': 'Alice',
            'user_id': alice_id,
            'title': 'Alice needs to post the HNS lock transaction.',
            'instructions': [
                'Verify the amount, hash, refund window, and HNS lock details.',
                'Broadcast the HNS lock from your wallet only after the details match.',
                'Record the HNS lock TXID in this room.'
            ],
        },
        'alice_locked': {
            'role': 'Bob',
            'user_id': bob_id,
            'title': "Bob needs to verify Alice's HNS lock and post the BTC lock.",
            'instructions': [
                "Verify Alice's HNS lock TXID, amount, hash, and confirmations.",
                'Broadcast the BTC lock only after the HNS lock checks out.',
                'Record the BTC lock TXID in this room.'
            ],
        },
        'bob_locked': {
            'role': 'Alice',
            'user_id': alice_id,
            'title': "Alice needs to verify Bob's BTC lock and claim BTC.",
            'instructions': [
                "Verify Bob's BTC lock TXID, amount, hash, and confirmations.",
                'Claim BTC with the secret when you are satisfied.',
                'Record the BTC claim TXID and revealed secret in this room.'
            ],
        },
        'alice_claimed': {
            'role': 'Bob',
            'user_id': bob_id,
            'title': 'Bob needs to claim HNS using the revealed secret.',
            'instructions': [
                "Verify Alice's BTC claim revealed the correct secret.",
                'Use the revealed secret to claim HNS.',
                'Record the HNS claim TXID to complete the swap.'
            ],
        },
    }
    return steps.get(swap.status)


def _complete_swap_reputation(swap):
    for user_id in _swap_participants(swap):
        user = User.query.get(user_id)
        if user:
            user.completed_swaps = (user.completed_swaps or 0) + 1


def _swap_role_label(swap, user_id):
    if user_id == _swap_alice_id(swap):
        return 'Alice'
    if user_id == _swap_bob_id(swap):
        return 'Bob'
    return 'Participant'


def _swap_user_label(user):
    return user.username if user else 'Unknown user'


def _increment_swap_reputation(user_id, field_name):
    user = User.query.get(user_id)
    if user and hasattr(user, field_name):
        setattr(user, field_name, (getattr(user, field_name) or 0) + 1)


def _external_scheme():
    if request.host.split(':', 1)[0] == 'liquidity.spot':
        return 'https'
    forwarded_proto = request.headers.get('X-Forwarded-Proto', '').split(',')[0].strip()
    if forwarded_proto in ['http', 'https']:
        return forwarded_proto
    return request.scheme


def _app_base_url():
    return f'{_external_scheme()}://{request.host}'.rstrip('/')


def _external_url_for(endpoint, **values):
    return url_for(endpoint, _external=True, _scheme=_external_scheme(), **values)


def _swap_public_payload(swap):
    return {
        'id': swap.id,
        'status': swap.status,
        'order_id': swap.order_id,
        'secret_hash': swap.secret_hash,
        'alice_user_id': _swap_alice_id(swap),
        'bob_user_id': _swap_bob_id(swap),
        'amount_hns': str(swap.order.amount_hns),
        'price_btc_per_hns': str(swap.order.price_btc_per_hns),
        'alice_lock_txid': swap.alice_lock_txid,
        'bob_lock_txid': swap.bob_lock_txid,
        'alice_claim_txid': swap.alice_claim_txid,
        'bob_claim_txid': swap.bob_claim_txid,
        'revealed_secret': swap.revealed_secret,
        'hns_htlc': {
            'claim_public_key': swap.hns_claim_public_key,
            'refund_public_key': swap.hns_refund_public_key,
            'refund_locktime': swap.hns_refund_locktime,
            'lock_address': swap.hns_lock_address,
            'lock_script': swap.hns_lock_script,
            'lock_value': swap.hns_lock_value,
            'lock_output_index': swap.hns_lock_output_index,
        },
        'verified': {
            'alice_lock': bool(swap.alice_lock_verified_at),
            'bob_lock': bool(swap.bob_lock_verified_at),
            'alice_claim': bool(swap.alice_claim_verified_at),
            'bob_claim': bool(swap.bob_claim_verified_at),
        },
        'adapter_error': swap.adapter_error,
        'wallet_intents_url': _external_url_for('main.api_swap_intents', id=swap.id) if request else None,
        'updated_at': swap.updated_at.isoformat() if swap.updated_at else None,
        'completed_at': swap.completed_at.isoformat() if swap.completed_at else None,
    }


def _ensure_adapter_token(swap):
    if not swap.adapter_token:
        swap.adapter_token = secrets.token_hex(32)
    return swap.adapter_token


def _adapter_token_valid(swap):
    supplied = request.args.get('token') or request.headers.get('X-Liquidity-Adapter-Token')
    return bool(swap.adapter_token and supplied and secrets.compare_digest(supplied, swap.adapter_token))


def _watch_swap_tx(swap, leg):
    if leg == 'alice_lock':
        if not swap.alice_lock_txid:
            return False, 'Alice HNS lock TXID has not been submitted yet.'
        result = verify_hns_tx(
            swap.alice_lock_txid,
            current_app.config.get('HNS_WATCHER_BASE_URL')
        )
        if not result.get('found'):
            return False, 'Alice HNS lock transaction was not found.'
        swap.alice_lock_verified_at = result['verified_at']
        return True, 'Alice HNS lock transaction found.'

    if leg == 'bob_lock':
        if not swap.bob_lock_txid:
            return False, 'Bob BTC lock TXID has not been submitted yet.'
        result = verify_bitcoin_tx(
            swap.bob_lock_txid,
            current_app.config.get('BTC_WATCHER_BASE_URL')
        )
        if not result.get('found'):
            return False, 'Bob BTC lock transaction was not found.'
        swap.bob_lock_verified_at = result['verified_at']
        return True, 'Bob BTC lock transaction found.'

    if leg == 'alice_claim':
        if not swap.alice_claim_txid:
            return False, 'Alice BTC claim TXID has not been submitted yet.'
        result = verify_bitcoin_tx(
            swap.alice_claim_txid,
            current_app.config.get('BTC_WATCHER_BASE_URL'),
            secret_hash=swap.secret_hash,
            require_secret=True
        )
        if not result.get('found'):
            return False, 'Alice BTC claim transaction was not found.'
        if result.get('secret'):
            swap.revealed_secret = result['secret']
            swap.alice_claim_verified_at = result['verified_at']
            return True, 'Alice BTC claim transaction found and secret verified.'
        return False, result.get('error') or 'Alice BTC claim transaction did not reveal the expected secret.'

    if leg == 'bob_claim':
        if not swap.bob_claim_txid:
            return False, 'Bob HNS claim TXID has not been submitted yet.'
        result = verify_hns_tx(
            swap.bob_claim_txid,
            current_app.config.get('HNS_WATCHER_BASE_URL')
        )
        if not result.get('found'):
            return False, 'Bob HNS claim transaction was not found.'
        swap.bob_claim_verified_at = result['verified_at']
        return True, 'Bob HNS claim transaction found.'

    return False, 'Unknown swap leg.'


def _watch_available(leg):
    if leg in ('alice_lock', 'bob_claim'):
        return bool(current_app.config.get('HNS_WATCHER_BASE_URL'))
    if leg in ('bob_lock', 'alice_claim'):
        return bool(current_app.config.get('BTC_WATCHER_BASE_URL'))
    return False


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

@main_bp.route('/bob-addon.json')
def bob_addon_manifest():
    return jsonify({
        'id': 'liquidity-spot',
        'name': 'Liquidity Spot',
        'publisher': 'LearnHNS',
        'version': '0.1.0',
        'description': 'P2P coordination for HNS/BTC-style liquidity trades.',
        'type': 'external-web',
        'entry': 'https://liquidity.spot/p2p',
        'homepage': 'https://liquidity.spot',
        'source': 'https://github.com/shadstoneofficial/liquidity-spot',
        'permissions': [],
        'capabilities': {
            'guestMode': True,
            'gfavipOptional': True,
            'gemsOptional': True,
            'walletCustody': False,
            'automaticSigning': False,
            'walletIntents': True,
            'bobHnsIntent': True,
            'bitcoinPsbtIntent': True,
            'chainWatcherCallbacks': True,
            'spvCompatible': True,
        },
        'adapterEndpoints': {
            'swapStatus': 'https://liquidity.spot/api/swaps/{swap_id}',
            'walletIntents': 'https://liquidity.spot/api/swaps/{swap_id}/intents',
            'submitAliceLock': 'https://liquidity.spot/api/swaps/{swap_id}/txids/alice-lock',
            'submitBobLock': 'https://liquidity.spot/api/swaps/{swap_id}/txids/bob-lock',
            'submitAliceClaim': 'https://liquidity.spot/api/swaps/{swap_id}/txids/alice-claim',
            'submitBobClaim': 'https://liquidity.spot/api/swaps/{swap_id}/txids/bob-claim',
        },
        'networks': ['main'],
        'status': 'public-preview',
    })


@main_bp.route('/api/channel', methods=['GET'])
def api_liquidity_channel():
    p2p_offers = P2POffer.query.filter_by(status='open').order_by(P2POffer.created_at.desc()).limit(25).all()
    atomic_orders = Order.query.filter_by(status='open').order_by(Order.created_at.desc()).limit(25).all()

    def user_payload(user):
        if not user:
            return {'id': None, 'username': 'Unknown'}
        return {
            'id': user.id,
            'username': user.username,
        }

    def timestamp(value):
        return value.isoformat() if value else None

    return jsonify({
        'id': 'liquidity-spot',
        'name': 'Liquidity.spot',
        'version': 1,
        'generated_at': datetime.utcnow().isoformat(),
        'links': {
            'home': _external_url_for('main.p2p'),
            'p2p': _external_url_for('main.p2p'),
            'atomic_orders': _external_url_for('main.orders'),
            'bob_download': 'https://bobwallet.org/download',
        },
        'requirements': {
            'bob_wallet': 'May 2026 experimental Bob Wallet build required for automatic HNS lock/claim actions.',
            'bob_wallet_download_url': 'https://bobwallet.org/download',
        },
        'p2p': {
            'offers': [
                {
                    'id': offer.id,
                    'creator': user_payload(offer.creator),
                    'side': offer.side,
                    'amount_hns': str(offer.amount_hns),
                    'price_btc_per_hns': str(offer.price_btc_per_hns),
                    'gems_stake': offer.gems_stake or 0,
                    'payment_method': offer.payment_method,
                    'notes': offer.notes,
                    'status': offer.status,
                    'created_at': timestamp(offer.created_at),
                    'url': _external_url_for('main.p2p') + f'#offer-{offer.id}',
                }
                for offer in p2p_offers
            ],
        },
        'atomic_swaps': {
            'orders': [
                {
                    'id': order.id,
                    'user': user_payload(order.user),
                    'side': order.side,
                    'amount_hns': str(order.amount_hns),
                    'price_btc_per_hns': str(order.price_btc_per_hns),
                    'gems_stake': order.gems_stake or 0,
                    'status': order.status,
                    'created_at': timestamp(order.created_at),
                    'url': _external_url_for('main.order_details', order_id=order.id),
                }
                for order in atomic_orders
            ],
        },
    })

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
    try:
        user = _ensure_session_user()
    except RuntimeError:
        flash('Could not start a guest session. Please try again.', 'error')
        return redirect(url_for('main.p2p'))

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
        db.session.rollback()
        flash(f'Error creating P2P offer: {exc}', 'error')
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error creating P2P offer')
        flash('Error creating P2P offer. Please check the details and try again.', 'error')

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

    active_swaps = [swap for swap in my_swaps if swap.status not in ['completed', 'canceled', 'refunded']]
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


@main_bp.route('/profile/notifications', methods=['POST'])
@login_required
def update_notification_preferences():
    user = User.query.get_or_404(session['user_id'])
    user.notify_email = bool(request.form.get('notify_email'))
    user.notify_telegram = bool(request.form.get('notify_telegram'))
    user.notify_wallet = bool(request.form.get('notify_wallet'))
    user.telegram_handle = (request.form.get('telegram_handle') or '').strip()[:80] or None

    try:
        db.session.commit()
        flash('Notification preferences saved.', 'success')
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error updating notification preferences')
        flash('Could not save notification preferences.', 'error')

    return redirect(url_for('main.profile'))


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
        try:
            user = _ensure_session_user()
        except RuntimeError:
            flash('Could not start a guest session. Please try again.', 'error')
            return redirect(url_for('main.orders'))

        side = request.form.get('side')
        amount_hns = request.form.get('amount_hns')
        price = request.form.get('price')
        gems_stake = _parse_gems_stake(request.form.get('gems_stake'))

        if side not in ('buy', 'sell'):
            flash('Order side must be buy or sell.', 'error')
            return redirect(url_for('main.orders'))

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
            if side == 'sell':
                flash('Order listed. Do not lock HNS yet; the HNS lock step starts after someone accepts and a swap room is created.', 'success')
            else:
                flash('Order listed. Do not lock BTC yet; the BTC lock step starts after an HNS seller accepts and posts the HNS lock.', 'success')
            return redirect(url_for('main.orders'))
        except (InvalidOperation, ValueError) as e:
            db.session.rollback()
            flash(f'Error creating order: {str(e)}', 'error')
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception('Error creating atomic swap order')
            flash('Error creating order. Please check the details and try again.', 'error')

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


@main_bp.route('/orders/<int:order_id>', methods=['GET'])
def order_details(order_id):
    order = Order.query.get_or_404(order_id)
    existing_swap = order.swap[0] if order.swap else None
    total_btc = Decimal(order.amount_hns) * Decimal(order.price_btc_per_hns)

    return render_template(
        'order_details.html',
        order=order,
        existing_swap=existing_swap,
        total_btc=total_btc,
    )


@main_bp.route('/orders/<int:order_id>/accept', methods=['POST'])
def accept_order(order_id):
    try:
        user = _ensure_session_user()
    except RuntimeError:
        flash('Could not start a guest session. Please try again.', 'error')
        return redirect(url_for('main.orders'))

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
        status=status,
        adapter_token=secrets.token_hex(32),
        latest_note='Swap matched. Waiting for Alice to generate the hash secret.' if status == 'pending_secret' else 'Swap initiated. Alice can now post the HNS lock transaction.'
    )
    
    order.status = 'matched'
    db.session.add(swap)
    db.session.commit()
    
    if status == 'initiated':
        flash('Swap initiated! You are Alice. Save your secret!', 'success')
    else:
        flash('Swap matched! Waiting for Alice (Maker) to generate secret.', 'info')
        
    return redirect(url_for('main.swap_details', id=swap.id))


@main_bp.route('/swaps')
def swaps_index():
    return redirect(url_for('main.orders'))

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
    swap.latest_note = 'Secret generated. Alice can now post the HNS lock transaction.'
    db.session.commit()
    
    session['generated_secret'] = secret
    flash('Secret generated! Swap is now live.', 'success')
    return redirect(url_for('main.swap_details', id=swap.id))


@main_bp.route('/api/swaps/<int:id>', methods=['GET'])
def api_swap_status(id):
    swap = Swap.query.get_or_404(id)
    return jsonify(_swap_public_payload(swap))


@main_bp.route('/api/swaps/<int:id>/intents', methods=['GET'])
@login_required
def api_swap_intents(id):
    swap = Swap.query.get_or_404(id)
    if session['user_id'] not in _swap_participants(swap):
        return jsonify({'error': 'You do not have permission to view wallet intents for this swap.'}), 403
    _ensure_adapter_token(swap)
    db.session.commit()
    return jsonify(build_swap_intents(swap, _app_base_url()))


@main_bp.route('/api/swaps/<int:id>/wallet-intents', methods=['GET'])
def api_wallet_swap_intents(id):
    swap = Swap.query.get_or_404(id)
    if not _adapter_token_valid(swap):
        return jsonify({'error': 'Invalid or missing adapter token.'}), 403
    return jsonify(build_swap_intents(swap, _app_base_url()))


@main_bp.route('/api/swaps/<int:id>/hns-htlc', methods=['POST'])
def api_update_hns_htlc_metadata(id):
    swap = Swap.query.get_or_404(id)
    if not _adapter_token_valid(swap):
        return jsonify({'error': 'Invalid or missing adapter token.'}), 403

    payload = request.get_json(silent=True) or request.form
    changed = []

    public_key_fields = {
        'hns_claim_public_key': 'hns_claim_public_key',
        'claim_public_key': 'hns_claim_public_key',
        'bob_hns_claim_public_key': 'hns_claim_public_key',
        'hns_refund_public_key': 'hns_refund_public_key',
        'refund_public_key': 'hns_refund_public_key',
        'alice_hns_refund_public_key': 'hns_refund_public_key',
    }
    for payload_key, field_name in public_key_fields.items():
        if payload.get(payload_key):
            value = _clean_public_key(payload.get(payload_key))
            if not value:
                return jsonify({'error': f'{payload_key} must be a compressed or uncompressed public key hex.'}), 400
            setattr(swap, field_name, value)
            changed.append(field_name)

    int_fields = {
        'hns_refund_locktime': 'hns_refund_locktime',
        'refund_locktime': 'hns_refund_locktime',
        'hns_lock_value': 'hns_lock_value',
        'htlc_value': 'hns_lock_value',
        'hns_lock_output_index': 'hns_lock_output_index',
        'lock_output_index': 'hns_lock_output_index',
    }
    for payload_key, field_name in int_fields.items():
        if payload.get(payload_key) is not None:
            value = _clean_non_negative_int(payload.get(payload_key))
            if value is None:
                return jsonify({'error': f'{payload_key} must be a non-negative integer.'}), 400
            setattr(swap, field_name, value)
            changed.append(field_name)

    address = (payload.get('hns_lock_address') or payload.get('htlc_address') or '').strip()
    if address:
        if len(address) > 128:
            return jsonify({'error': 'hns_lock_address is too long.'}), 400
        swap.hns_lock_address = address
        changed.append('hns_lock_address')

    script = payload.get('hns_lock_script') or payload.get('htlc_script')
    if script:
        value = _clean_script(script)
        if not value:
            return jsonify({'error': 'hns_lock_script must be hex and at most 4096 characters.'}), 400
        swap.hns_lock_script = value
        changed.append('hns_lock_script')

    if not changed:
        return jsonify({'error': 'No HNS HTLC metadata fields were supplied.'}), 400

    swap.latest_note = 'HNS HTLC wallet metadata updated.'

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error recording HNS HTLC metadata')
        return jsonify({'error': 'Could not record HNS HTLC metadata.'}), 500

    return jsonify({
        'changed': sorted(set(changed)),
        'swap': _swap_public_payload(swap),
    })


@main_bp.route('/api/swaps/<int:id>/txids/<leg>', methods=['POST'])
def api_submit_swap_txid(id, leg):
    swap = Swap.query.get_or_404(id)
    if not _adapter_token_valid(swap):
        return jsonify({'error': 'Invalid or missing adapter token.'}), 403

    payload = request.get_json(silent=True) or request.form
    txid = _clean_txid(payload.get('txid'))
    note = (payload.get('note') or '').strip()

    leg_map = {
        'alice-lock': ('alice_lock_txid', 'alice_locked', 'Alice HNS lock transaction submitted by wallet adapter.'),
        'bob-lock': ('bob_lock_txid', 'bob_locked', 'Bob BTC lock transaction submitted by wallet adapter.'),
        'alice-claim': ('alice_claim_txid', 'alice_claimed', 'Alice BTC claim transaction submitted by wallet adapter.'),
        'bob-claim': ('bob_claim_txid', 'completed', 'Bob HNS claim transaction submitted by wallet adapter.'),
    }
    if leg not in leg_map:
        return jsonify({'error': 'Unknown swap leg.'}), 404
    if not txid:
        return jsonify({'error': 'A valid 64-character txid is required.'}), 400

    field_name, next_status, default_note = leg_map[leg]

    if leg == 'alice-lock' and swap.status not in ['initiated', 'alice_locked']:
        return jsonify({'error': 'Alice lock cannot be submitted before the swap is initiated.'}), 409
    if leg == 'bob-lock' and swap.status not in ['alice_locked', 'bob_locked']:
        return jsonify({'error': 'Bob lock cannot be submitted before Alice lock.'}), 409
    if leg == 'alice-claim' and swap.status not in ['bob_locked', 'alice_claimed']:
        return jsonify({'error': 'Alice claim cannot be submitted before Bob lock.'}), 409
    if leg == 'bob-claim' and swap.status not in ['alice_claimed', 'completed']:
        return jsonify({'error': 'Bob claim cannot be submitted before Alice claim.'}), 409

    if leg == 'alice-claim':
        secret = _clean_secret(payload.get('revealed_secret'))
        if not secret or _hash_secret(secret) != swap.secret_hash:
            return jsonify({'error': 'revealed_secret does not match this swap hash.'}), 400
        swap.revealed_secret = secret

    if leg == 'alice-lock':
        hns_metadata = {
            'hns_lock_output_index': payload.get('hns_lock_output_index') or payload.get('lock_output_index'),
            'hns_lock_value': payload.get('hns_lock_value') or payload.get('htlc_value'),
        }
        for field_name, raw_value in hns_metadata.items():
            if raw_value is not None:
                value = _clean_non_negative_int(raw_value)
                if value is None:
                    return jsonify({'error': f'{field_name} must be a non-negative integer.'}), 400
                setattr(swap, field_name, value)

        address = (payload.get('hns_lock_address') or payload.get('htlc_address') or '').strip()
        if address:
            if len(address) > 128:
                return jsonify({'error': 'hns_lock_address is too long.'}), 400
            swap.hns_lock_address = address

        script = payload.get('hns_lock_script') or payload.get('htlc_script')
        if script:
            value = _clean_script(script)
            if not value:
                return jsonify({'error': 'hns_lock_script must be hex and at most 4096 characters.'}), 400
            swap.hns_lock_script = value

    setattr(swap, field_name, txid)
    if leg == 'bob-claim' and swap.status != 'completed':
        _complete_swap_reputation(swap)
        swap.completed_at = datetime.utcnow()
    swap.status = next_status
    swap.latest_note = note or default_note

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error recording wallet adapter TXID')
        return jsonify({'error': 'Could not record TXID.'}), 500

    return jsonify(_swap_public_payload(swap))


@main_bp.route('/api/swaps/<int:id>/verify/<leg>', methods=['POST'])
def api_verify_swap_leg(id, leg):
    swap = Swap.query.get_or_404(id)
    leg = leg.replace('-', '_')

    if not _watch_available(leg):
        return jsonify({'verified': False, 'message': 'Watcher for this chain is not configured.'}), 503

    try:
        verified, message = _watch_swap_tx(swap, leg)
        swap.adapter_error = None if verified else message
        db.session.commit()
    except WatcherError as exc:
        db.session.rollback()
        return jsonify({'verified': False, 'message': str(exc)}), 502
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error verifying swap leg')
        return jsonify({'verified': False, 'message': 'Could not save verification result.'}), 500

    return jsonify({
        'verified': verified,
        'message': message,
        'swap': _swap_public_payload(swap),
    })


@main_bp.route('/swaps/<int:id>/verify/<leg>', methods=['POST'])
@login_required
def verify_swap_leg(id, leg):
    swap = Swap.query.get_or_404(id)
    leg = leg.replace('-', '_')

    if session['user_id'] not in _swap_participants(swap):
        flash('You do not have permission to verify this swap.', 'error')
        return redirect(url_for('main.dashboard'))

    if not _watch_available(leg):
        flash('Watcher for this chain is not configured yet.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    try:
        verified, message = _watch_swap_tx(swap, leg)
        swap.adapter_error = None if verified else message
        db.session.commit()
        flash(message, 'success' if verified else 'warning')
    except WatcherError as exc:
        db.session.rollback()
        flash(f'Watcher error: {exc}', 'error')
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error verifying swap leg')
        flash('Could not save verification result.', 'error')

    return redirect(url_for('main.swap_details', id=swap.id))


@main_bp.route('/swaps/<int:id>/progress', methods=['POST'])
@login_required
def progress_swap(id):
    swap = Swap.query.get_or_404(id)
    user_id = session['user_id']

    if user_id not in _swap_participants(swap):
        flash('You do not have permission to update this swap.', 'error')
        return redirect(url_for('main.dashboard'))

    action = request.form.get('action')
    note = (request.form.get('note') or '').strip()

    try:
        if action == 'post_alice_lock':
            if not _is_swap_alice(swap, user_id):
                flash('Only Alice can post the HNS lock transaction.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))
            if swap.status not in ['initiated', 'alice_locked']:
                flash('Alice can only post the HNS lock before Bob locks BTC.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))

            txid = _clean_txid(request.form.get('alice_lock_txid'))
            if not txid:
                flash('Enter a valid 64-character HNS lock TXID.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))

            swap.alice_lock_txid = txid
            swap.status = 'alice_locked'
            swap.latest_note = note or 'Alice posted the HNS lock transaction. Bob should verify it before locking BTC.'

        elif action == 'post_bob_lock':
            if not _is_swap_bob(swap, user_id):
                flash('Only Bob can post the BTC lock transaction.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))
            if swap.status not in ['alice_locked', 'bob_locked']:
                flash('Bob should only lock BTC after Alice posts the HNS lock TXID.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))

            txid = _clean_txid(request.form.get('bob_lock_txid'))
            if not txid:
                flash('Enter a valid 64-character BTC lock TXID.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))

            swap.bob_lock_txid = txid
            swap.status = 'bob_locked'
            swap.latest_note = note or 'Bob posted the BTC lock transaction. Alice can claim BTC and reveal the secret.'

        elif action == 'post_alice_claim':
            if not _is_swap_alice(swap, user_id):
                flash('Only Alice can post the BTC claim transaction.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))
            if swap.status not in ['bob_locked', 'alice_claimed']:
                flash('Alice can claim BTC only after Bob posts the BTC lock TXID.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))

            txid = _clean_txid(request.form.get('alice_claim_txid'))
            secret = _clean_secret(request.form.get('revealed_secret'))
            if not txid:
                flash('Enter a valid 64-character BTC claim TXID.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))
            if not secret or _hash_secret(secret) != swap.secret_hash:
                flash('The revealed secret does not match this swap hash.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))

            swap.alice_claim_txid = txid
            swap.revealed_secret = secret
            swap.status = 'alice_claimed'
            swap.latest_note = note or 'Alice claimed BTC and revealed the secret. Bob can now claim HNS.'

        elif action == 'post_bob_claim':
            if not _is_swap_bob(swap, user_id):
                flash('Only Bob can post the HNS claim transaction.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))
            if swap.status not in ['alice_claimed', 'completed']:
                flash('Bob can claim HNS only after Alice reveals the secret on the BTC claim.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))

            txid = _clean_txid(request.form.get('bob_claim_txid'))
            if not txid:
                flash('Enter a valid 64-character HNS claim TXID.', 'error')
                return redirect(url_for('main.swap_details', id=swap.id))

            swap.bob_claim_txid = txid
            if swap.status != 'completed':
                _complete_swap_reputation(swap)
            swap.status = 'completed'
            swap.completed_at = datetime.utcnow()
            swap.latest_note = note or 'Bob claimed HNS. Swap completed.'

        elif action == 'post_refund':
            if user_id == _swap_alice_id(swap):
                txid = _clean_txid(request.form.get('alice_refund_txid'))
                if not txid:
                    flash('Enter a valid 64-character HNS refund TXID.', 'error')
                    return redirect(url_for('main.swap_details', id=swap.id))
                swap.alice_refund_txid = txid
                swap.latest_note = note or 'Alice posted an HNS refund transaction.'
            else:
                txid = _clean_txid(request.form.get('bob_refund_txid'))
                if not txid:
                    flash('Enter a valid 64-character BTC refund TXID.', 'error')
                    return redirect(url_for('main.swap_details', id=swap.id))
                swap.bob_refund_txid = txid
                swap.latest_note = note or 'Bob posted a BTC refund transaction.'
            swap.status = 'refunded'
            swap.completed_at = datetime.utcnow()

        else:
            flash('Unknown swap action.', 'error')
            return redirect(url_for('main.swap_details', id=swap.id))

        db.session.commit()
        flash('Swap progress updated.', 'success')
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error updating atomic swap progress')
        flash('Could not update swap progress. Please try again.', 'error')

    return redirect(url_for('main.swap_details', id=swap.id))


@main_bp.route('/swaps/<int:id>/message', methods=['POST'])
@login_required
def add_swap_message(id):
    swap = Swap.query.get_or_404(id)
    user_id = session['user_id']

    if user_id not in _swap_participants(swap):
        flash('You do not have permission to post in this swap.', 'error')
        return redirect(url_for('main.dashboard'))

    message = (request.form.get('message') or '').strip()
    if not message:
        flash('Message cannot be empty.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    duplicate_window = datetime.utcnow() - timedelta(seconds=15)
    recent_duplicate = SwapMessage.query.filter(
        SwapMessage.swap_id == swap.id,
        SwapMessage.user_id == user_id,
        SwapMessage.message == message,
        SwapMessage.created_at >= duplicate_window
    ).first()
    if recent_duplicate:
        flash('Looks like that swap note was already posted.', 'info')
        return redirect(url_for('main.swap_details', id=swap.id))

    db.session.add(SwapMessage(
        swap_id=swap.id,
        user_id=user_id,
        message=message
    ))
    try:
        db.session.commit()
        flash('Swap note added.', 'success')
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error adding atomic swap note')
        flash('Could not add swap note. Please try again.', 'error')

    return redirect(url_for('main.swap_details', id=swap.id))


@main_bp.route('/swaps/<int:id>/send-reminder', methods=['POST'])
@login_required
def send_swap_reminder(id):
    swap = Swap.query.get_or_404(id)
    user_id = session['user_id']

    if user_id not in _swap_participants(swap):
        flash('You do not have permission to send reminders for this swap.', 'error')
        return redirect(url_for('main.dashboard'))

    next_step = _swap_next_step(swap)
    if not next_step or swap.status in ['completed', 'refunded', 'canceled', 'disputed']:
        flash('This swap does not need a reminder right now.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    if user_id == next_step['user_id']:
        flash('You are the next actor. Post an update or complete the step instead of reminding yourself.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    now = datetime.utcnow()
    pending_since = swap.updated_at or swap.created_at or now
    reminder_at = pending_since + timedelta(hours=SWAP_REMINDER_HOURS)
    reminder_repeat_at = (swap.last_reminder_at or datetime.min) + timedelta(hours=SWAP_REMINDER_REPEAT_HOURS)

    if now < reminder_at:
        flash(f'Reminders unlock after {SWAP_REMINDER_HOURS} hours with no step progress.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    if swap.last_reminder_at and now < reminder_repeat_at:
        flash(f'Another reminder can be sent after {SWAP_REMINDER_REPEAT_HOURS} hours.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    actor = User.query.get(next_step['user_id'])
    sender_role = _swap_role_label(swap, user_id)
    actor_label = f"{next_step['role']} ({_swap_user_label(actor)})"
    message = (
        f'Reminder from {sender_role}: {actor_label} is next for swap #{swap.id}. '
        f'Please complete the step or leave a status note.'
    )
    swap.last_reminder_at = now
    swap.latest_note = message
    db.session.add(SwapMessage(swap_id=swap.id, user_id=user_id, message=message))

    try:
        db.session.commit()
        flash('Reminder posted in the swap room.', 'success')
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error sending atomic swap reminder')
        flash('Could not send reminder. Please try again.', 'error')

    return redirect(url_for('main.swap_details', id=swap.id))


@main_bp.route('/swaps/<int:id>/timeout-cancel', methods=['POST'])
@login_required
def timeout_cancel_swap(id):
    swap = Swap.query.get_or_404(id)
    user_id = session['user_id']

    if user_id not in _swap_participants(swap):
        flash('You do not have permission to cancel this swap.', 'error')
        return redirect(url_for('main.dashboard'))

    next_step = _swap_next_step(swap)
    pending_since = swap.updated_at or swap.created_at or datetime.utcnow()
    timeout_at = pending_since + timedelta(hours=SWAP_STALE_CANCEL_HOURS)
    funds_recorded = bool(swap.alice_lock_txid or swap.bob_lock_txid or swap.alice_claim_txid or swap.bob_claim_txid)

    if not next_step or swap.status in ['completed', 'refunded', 'canceled']:
        flash('This swap is already in a final state.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    if funds_recorded or swap.status not in ['pending_secret', 'initiated']:
        flash('This swap has moved beyond the safe cancel window. Use the refund path or messages instead.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    if user_id == next_step['user_id']:
        flash('You are the next actor for this swap. Cancel is only available to the waiting counterparty after the timeout.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    if datetime.utcnow() < timeout_at:
        flash(f'Timeout cancel unlocks after {SWAP_STALE_CANCEL_HOURS} hours with no step progress.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    _increment_swap_reputation(next_step['user_id'], 'stale_cancellations')
    _increment_swap_reputation(next_step['user_id'], 'stale_no_shows')
    swap.status = 'canceled'
    swap.latest_note = f'Swap canceled after {SWAP_STALE_CANCEL_HOURS} hours without the required next step.'
    swap.completed_at = datetime.utcnow()
    db.session.add(SwapMessage(
        swap_id=swap.id,
        user_id=user_id,
        message='Canceled after timeout because the required next step was not completed.'
    ))
    db.session.commit()
    flash('Swap canceled after timeout.', 'success')
    return redirect(url_for('main.swap_details', id=swap.id))


@main_bp.route('/swaps/<int:id>/request-review', methods=['POST'])
@login_required
def request_swap_review(id):
    swap = Swap.query.get_or_404(id)
    user_id = session['user_id']

    if user_id not in _swap_participants(swap):
        flash('You do not have permission to request review for this swap.', 'error')
        return redirect(url_for('main.dashboard'))

    next_step = _swap_next_step(swap)
    pending_since = swap.updated_at or swap.created_at or datetime.utcnow()
    timeout_at = pending_since + timedelta(hours=SWAP_STALE_CANCEL_HOURS)
    funds_recorded = bool(swap.alice_lock_txid or swap.bob_lock_txid or swap.alice_claim_txid or swap.bob_claim_txid)
    note = (request.form.get('note') or '').strip()

    if not next_step or swap.status in ['completed', 'refunded', 'canceled', 'disputed']:
        flash('This swap is already in a final or review state.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    if not funds_recorded:
        flash('No lock or claim TXID has been recorded yet. Use timeout cancel before funds are locked.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    if user_id == next_step['user_id']:
        flash('You are the next actor for this swap. Post an update or complete the required step instead.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    if datetime.utcnow() < timeout_at:
        flash(f'Review request unlocks after {SWAP_STALE_CANCEL_HOURS} hours with no step progress.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    _increment_swap_reputation(next_step['user_id'], 'disputed_swaps')
    swap.status = 'disputed'
    swap.admin_review_status = 'in_review'
    swap.admin_resolution = 'disputed'
    swap.latest_note = 'Review requested after the post-lock step timed out.'
    db.session.add(SwapMessage(
        swap_id=swap.id,
        user_id=user_id,
        message=note or 'Requested review after timeout because the required post-lock step was not completed.'
    ))
    db.session.commit()
    flash('Swap review requested.', 'success')
    return redirect(url_for('main.swap_details', id=swap.id))


@main_bp.route('/swaps/<int:id>/relist', methods=['POST'])
@login_required
def relist_swap_order(id):
    swap = Swap.query.get_or_404(id)
    user_id = session['user_id']

    if user_id not in _swap_participants(swap):
        flash('You do not have permission to relist from this swap.', 'error')
        return redirect(url_for('main.dashboard'))

    if swap.status != 'canceled':
        flash('Only canceled swaps can be relisted.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    side = (request.form.get('side') or '').strip().lower()
    amount_hns = request.form.get('amount_hns')
    price = request.form.get('price')
    try:
        amount_value = Decimal(amount_hns)
        price_value = Decimal(price)
        if amount_value <= 0 or price_value <= 0:
            raise InvalidOperation()
    except (InvalidOperation, TypeError, ValueError):
        flash('Enter a valid refreshed HNS amount and BTC/HNS price before relisting.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    if side not in ['buy', 'sell']:
        flash('Choose whether the new order is buying or selling HNS.', 'error')
        return redirect(url_for('main.swap_details', id=swap.id))

    order = Order(
        user_id=user_id,
        side=side,
        amount_hns=amount_value,
        price_btc_per_hns=price_value,
        gems_stake=swap.order.gems_stake,
        status='open'
    )
    db.session.add(order)
    db.session.add(SwapMessage(
        swap_id=swap.id,
        user_id=user_id,
        message=f'Relisted a new {side.upper()} order for {amount_value} HNS at {price_value} BTC/HNS after reviewing the market.'
    ))
    db.session.commit()
    flash('New order listed with refreshed price.', 'success')
    return redirect(url_for('main.orders'))

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
    if not user:
        session.clear()
        flash('Your session expired. Please open the swap again.', 'error')
        return redirect(url_for('main.index'))
    
    # Check if user is involved
    if user.id != swap.order.user_id and user.id != swap.matcher_id:
        flash('You do not have permission to view this swap.', 'error')
        return redirect(url_for('main.dashboard'))
        
    secret = session.get('generated_secret') if swap.role_alice_user_id == user.id else None
    is_alice = _is_swap_alice(swap, user.id)
    bob_user = User.query.get(_swap_bob_id(swap))
    alice_user = User.query.get(_swap_alice_id(swap))
    next_step = _swap_next_step(swap)
    next_actor_user = User.query.get(next_step['user_id']) if next_step else None
    current_swap_role = _swap_role_label(swap, user.id)
    pending_since = swap.updated_at or swap.created_at or datetime.utcnow()
    timeout_at = pending_since + timedelta(hours=SWAP_STALE_CANCEL_HOURS)
    reminder_at = pending_since + timedelta(hours=SWAP_REMINDER_HOURS)
    reminder_repeat_at = (swap.last_reminder_at or datetime.min) + timedelta(hours=SWAP_REMINDER_REPEAT_HOURS)
    now = datetime.utcnow()
    funds_recorded = bool(swap.alice_lock_txid or swap.bob_lock_txid or swap.alice_claim_txid or swap.bob_claim_txid)
    can_timeout_cancel = (
        next_step
        and not funds_recorded
        and swap.status in ['pending_secret', 'initiated']
        and user.id != next_step['user_id']
        and now >= timeout_at
    )
    can_request_review = (
        next_step
        and funds_recorded
        and swap.status not in ['completed', 'refunded', 'canceled', 'disputed']
        and user.id != next_step['user_id']
        and now >= timeout_at
    )
    can_send_reminder = (
        next_step
        and swap.status not in ['completed', 'refunded', 'canceled', 'disputed']
        and user.id != next_step['user_id']
        and now >= reminder_at
        and (not swap.last_reminder_at or now >= reminder_repeat_at)
    )
    default_relist_side = 'sell' if is_alice else 'buy'
    if not swap.adapter_token:
        adapter_token = secrets.token_hex(32)
        db.session.execute(
            text("UPDATE swaps SET adapter_token = :adapter_token, updated_at = :updated_at WHERE id = :swap_id"),
            {'adapter_token': adapter_token, 'updated_at': swap.updated_at, 'swap_id': swap.id}
        )
        db.session.commit()
        db.session.refresh(swap)
    else:
        adapter_token = swap.adapter_token
    wallet_intents_url = _external_url_for(
        'main.api_wallet_swap_intents',
        id=swap.id,
        token=adapter_token
    )
    bob_deeplink_url = 'bob://liquidityswap?intent=' + wallet_intents_url
    
    # Get coingecko price (mock for now or real request)
    try:
        # response = requests.get('https://api.coingecko.com/api/v3/simple/price?ids=handshake&vs_currencies=btc')
        # price = response.json()['handshake']['btc']
        price = 0.000000500000 # Placeholder
    except:
        price = 0.00000000
        
    return render_template(
        'swap.html',
        swap=swap,
        user=user,
        secret=secret,
        coingecko_price=price,
        is_alice=is_alice,
        alice_user=alice_user,
        bob_user=bob_user,
        hns_watcher_configured=bool(current_app.config.get('HNS_WATCHER_BASE_URL')),
        btc_watcher_configured=bool(current_app.config.get('BTC_WATCHER_BASE_URL')),
        wallet_intents_url=wallet_intents_url,
        bob_deeplink_url=bob_deeplink_url,
        next_step=next_step,
        next_actor_user=next_actor_user,
        current_swap_role=current_swap_role,
        pending_since=pending_since,
        timeout_at=timeout_at,
        reminder_at=reminder_at,
        stale_cancel_hours=SWAP_STALE_CANCEL_HOURS,
        reminder_hours=SWAP_REMINDER_HOURS,
        can_timeout_cancel=can_timeout_cancel,
        can_request_review=can_request_review,
        can_send_reminder=can_send_reminder,
        funds_recorded=funds_recorded,
        default_relist_side=default_relist_side,
    )
