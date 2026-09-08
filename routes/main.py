from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, Response, jsonify
from models import db, User, Order, Swap, SwapMessage, P2POffer, P2PTrade, P2PTradeMessage, P2PTradeParticipantState, P2PTradeFeedback
import os
from routes.auth import attach_guest_recovery_token, login_required
import secrets
import hashlib
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
from services.http_client import get as http_get

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
            attach_guest_recovery_token(user)
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


def _current_hns_btc_price():
    try:
        response = http_get(
            'https://api.coingecko.com/api/v3/simple/price?ids=handshake&vs_currencies=btc',
            timeout=8,
        )
        if response.status_code == 200:
            return response.json().get('handshake', {}).get('btc', 0.000000500000)
    except Exception as e:
        print(f"CoinGecko API Error: {e}")
    return 0.000000500000


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


def _p2p_offer_context(offer):
    total_btc = Decimal(offer.amount_hns) * Decimal(offer.price_btc_per_hns)
    total_sats = (total_btc * Decimal('100000000')).quantize(
        Decimal('1'), rounding=ROUND_HALF_UP
    )

    if offer.side == 'buy':
        return {
            'creator_role': 'HNS buyer',
            'counterparty_role': 'HNS seller',
            'waiting_label': 'Waiting for an HNS seller',
            'action_label': 'Sell HNS to this buyer',
            'counterparty_explanation': (
                'The offer creator has BTC and wants HNS. Another person with HNS '
                'must accept this offer before a swap can begin.'
            ),
            'total_btc': total_btc,
            'total_sats': total_sats,
        }

    return {
        'creator_role': 'HNS seller',
        'counterparty_role': 'HNS buyer',
        'waiting_label': 'Waiting for an HNS buyer',
        'action_label': 'Buy HNS from this seller',
        'counterparty_explanation': (
            'The offer creator has HNS and wants BTC. Another person with BTC '
            'must accept this offer before a swap can begin.'
        ),
        'total_btc': total_btc,
        'total_sats': total_sats,
    }


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


def _p2p_trade_parties(trade):
    if trade.offer.side == 'sell':
        alice_user = trade.creator
        bob_user = trade.counterparty
    else:
        alice_user = trade.counterparty
        bob_user = trade.creator
    return alice_user, bob_user


def _p2p_counterparty(trade, user_id):
    return trade.counterparty if user_id == trade.creator_id else trade.creator


def _p2p_feedback_stats(user_id):
    feedback = P2PTradeFeedback.query.filter_by(reviewee_id=user_id).all()
    completed_trades = P2PTrade.query.filter(
        ((P2PTrade.creator_id == user_id) | (P2PTrade.counterparty_id == user_id)) &
        (P2PTrade.status == 'completed')
    ).count()

    if not feedback:
        return {
            'count': 0,
            'average': None,
            'positive_count': 0,
            'completed_trades': completed_trades,
        }

    rating_total = sum(item.rating for item in feedback)
    return {
        'count': len(feedback),
        'average': round(rating_total / len(feedback), 1),
        'positive_count': sum(1 for item in feedback if item.rating >= 4),
        'completed_trades': completed_trades,
    }


def _build_p2p_trade_receipt_text(trade, requested_by=None):
    alice_user, bob_user = _p2p_trade_parties(trade)
    requested_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    created_at = trade.created_at.strftime('%Y-%m-%d %H:%M UTC') if trade.created_at else 'Unknown'
    updated_at = trade.updated_at.strftime('%Y-%m-%d %H:%M UTC') if trade.updated_at else 'Unknown'
    total_btc = Decimal(trade.offer.amount_hns) * Decimal(trade.offer.price_btc_per_hns)

    lines = [
        'Liquidity.spot P2P Trade Receipt',
        '=' * 36,
        f'Trade ID: #{trade.id}',
        f'Offer ID: #{trade.offer_id}',
        f'Generated: {requested_at}',
        f'Requested by: {requested_by.username if requested_by else "Unknown"}',
        '',
        'Parties',
        '-' * 36,
        f'Alice (HNS seller): {alice_user.username}',
        f'Bob (HNS buyer / BTC seller): {bob_user.username}',
        f'Offer creator: {trade.creator.username}',
        f'Counterparty: {trade.counterparty.username}',
        '',
        'Trade Terms',
        '-' * 36,
        f'Side: {trade.offer.side.upper()} HNS',
        f'Amount: {trade.offer.amount_hns} HNS',
        f'Price: {trade.offer.price_btc_per_hns} BTC/HNS',
        f'Total BTC: {total_btc:.12f}',
        f'Payment method: {trade.offer.payment_method}',
        f'Gems bond: {trade.maker_bond_amount or 0}',
        f'Maker bond status: {trade.maker_bond_status}',
        '',
        'Current State',
        '-' * 36,
        f'Status: {trade.status}',
        f'Milestone: {trade.milestone}',
        f'Latest note: {trade.latest_note or "None"}',
        f'Created: {created_at}',
        f'Updated: {updated_at}',
        f'Admin review: {trade.admin_review_status}',
        f'Admin resolution: {trade.admin_resolution or "Pending"}',
    ]

    if trade.admin_notes:
        lines.append(f'Admin notes: {trade.admin_notes}')

    lines.extend([
        '',
        'Transaction Records',
        '-' * 36,
        f'Alice lock TXID: {trade.alice_lock_txid or "Not submitted"}',
        f'Bob lock TXID: {trade.bob_lock_txid or "Not submitted"}',
    ])

    if trade.offer.notes:
        lines.extend([
            '',
            'Offer Notes',
            '-' * 36,
            trade.offer.notes,
        ])

    lines.extend([
        '',
        'Message Transcript',
        '-' * 36,
    ])

    messages = sorted(trade.messages, key=lambda message: message.created_at)
    if not messages:
        lines.append('No messages recorded.')
    else:
        for message in messages:
            when = message.created_at.strftime('%Y-%m-%d %H:%M UTC') if message.created_at else 'Unknown time'
            lines.append(f'[{when}] {message.user.username}:')
            lines.append(message.message)
            lines.append('')

    lines.extend([
        '',
        'Counterparty Feedback',
        '-' * 36,
    ])
    feedback_items = sorted(trade.feedback, key=lambda item: item.created_at)
    if not feedback_items:
        lines.append('No feedback recorded.')
    else:
        for feedback in feedback_items:
            when = feedback.created_at.strftime('%Y-%m-%d %H:%M UTC') if feedback.created_at else 'Unknown time'
            lines.append(f'[{when}] {feedback.reviewer.username} rated {feedback.reviewee.username}: {feedback.rating}/5')
            if feedback.comment:
                lines.append(feedback.comment)
            lines.append('')

    lines.extend([
        '',
        'Record Note',
        '-' * 36,
        'This receipt is generated from the Liquidity.spot trade room record. '
        'Participants remain responsible for verifying counterparties, transaction IDs, confirmations, and settlement.'
    ])

    return '\n'.join(lines).strip() + '\n'


def _pdf_escape(value):
    safe = ''.join(char if ord(char) < 256 else '?' for char in value)
    return safe.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _pdf_color(hex_color):
    hex_value = hex_color.lstrip('#')
    red = int(hex_value[0:2], 16) / 255
    green = int(hex_value[2:4], 16) / 255
    blue = int(hex_value[4:6], 16) / 255
    return f'{red:.3f} {green:.3f} {blue:.3f}'


def _pdf_text(commands, x, y, text, size=10, font='F1', color='#111111', leading=None):
    commands.append(f'{_pdf_color(color)} rg')
    commands.append('BT')
    commands.append(f'/{font} {size} Tf')
    if leading:
        commands.append(f'{leading} TL')
    commands.append(f'{x} {y} Td')
    commands.append(f'({_pdf_escape(str(text))}) Tj')
    commands.append('ET')


def _pdf_rect(commands, x, y, width, height, fill=None, stroke=None, stroke_width=1):
    if fill:
        commands.append(f'{_pdf_color(fill)} rg')
    if stroke:
        commands.append(f'{_pdf_color(stroke)} RG')
        commands.append(f'{stroke_width} w')
    operator = 'B' if fill and stroke else 'f' if fill else 'S'
    commands.append(f'{x} {y} {width} {height} re {operator}')


def _pdf_line(commands, x1, y1, x2, y2, color='#d2d0d2', stroke_width=1):
    commands.append(f'{_pdf_color(color)} RG')
    commands.append(f'{stroke_width} w')
    commands.append(f'{x1} {y1} m {x2} {y2} l S')


def _wrap_pdf_lines(text, max_chars):
    wrapped = []
    for raw_line in str(text or '').splitlines() or ['']:
        line = raw_line.strip()
        if not line:
            wrapped.append('')
            continue
        while len(line) > max_chars:
            split_at = line.rfind(' ', 0, max_chars)
            if split_at < 24:
                split_at = max_chars
            wrapped.append(line[:split_at])
            line = line[split_at:].lstrip()
        wrapped.append(line)
    return wrapped


def _build_pdf_document(title, page_commands):
    objects = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        '',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>'
    ]
    page_refs = []

    for commands in page_commands:
        stream = '\n'.join(commands)
        stream_bytes = stream.encode('latin-1', errors='replace')
        page_object_number = len(objects) + 1
        content_object_number = page_object_number + 1
        page_refs.append(f'{page_object_number} 0 R')
        objects.append(
            f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            f'/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> '
            f'/Contents {content_object_number} 0 R >>'
        )
        objects.append(f'<< /Length {len(stream_bytes)} >>\nstream\n{stream}\nendstream')

    objects[1] = f'<< /Type /Pages /Kids [{" ".join(page_refs)}] /Count {len(page_refs)} >>'

    pdf = ['%PDF-1.4\n%\xe2\xe3\xcf\xd3\n']
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(part.encode('latin-1', errors='replace')) for part in pdf))
        pdf.append(f'{index} 0 obj\n{obj}\nendobj\n')

    xref_offset = sum(len(part.encode('latin-1', errors='replace')) for part in pdf)
    pdf.append(f'xref\n0 {len(objects) + 1}\n')
    pdf.append('0000000000 65535 f \n')
    for offset in offsets[1:]:
        pdf.append(f'{offset:010d} 00000 n \n')
    pdf.append(
        f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Title ({_pdf_escape(title)}) >>\n'
        f'startxref\n{xref_offset}\n%%EOF\n'
    )
    return ''.join(pdf).encode('latin-1', errors='replace')


def _build_p2p_trade_receipt_pdf(trade, requested_by=None):
    alice_user, bob_user = _p2p_trade_parties(trade)
    generated_at = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    created_at = trade.created_at.strftime('%Y-%m-%d %H:%M UTC') if trade.created_at else 'Unknown'
    updated_at = trade.updated_at.strftime('%Y-%m-%d %H:%M UTC') if trade.updated_at else 'Unknown'
    amount_hns = Decimal(trade.offer.amount_hns)
    price_btc = Decimal(trade.offer.price_btc_per_hns)
    total_btc = amount_hns * price_btc
    status_colors = {
        'matched': '#15689e',
        'completed': '#29b973',
        'disputed': '#b91c1c',
        'canceled': '#6b7280',
        'no_show': '#c2410c',
    }
    status_color = status_colors.get(trade.status, '#15689e')

    pages = []
    commands = []
    y = 0
    page_number = 0

    def start_page():
        nonlocal commands, y, page_number
        if commands:
            _pdf_text(commands, 52, 30, 'Liquidity.spot receipt - generated from the trade room record', 8, 'F1', '#6b7280')
            _pdf_text(commands, 530, 30, f'Page {page_number}', 8, 'F1', '#6b7280')
            pages.append(commands)
        page_number += 1
        commands = []
        _pdf_rect(commands, 0, 0, 612, 792, fill='#ffffff')
        _pdf_rect(commands, 0, 724, 612, 68, fill='#124e2c')
        _pdf_rect(commands, 0, 712, 612, 12, fill='#29b973')
        _pdf_text(commands, 52, 756, 'Liquidity.spot', 22, 'F2', '#ffffff')
        _pdf_text(commands, 52, 738, 'P2P trade receipt', 11, 'F1', '#d2d0d2')
        _pdf_rect(commands, 438, 744, 122, 24, fill=status_color)
        _pdf_text(commands, 456, 752, trade.status.upper(), 10, 'F2', '#ffffff')
        y = 676

    def ensure_space(required_height):
        nonlocal y
        if y - required_height < 62:
            start_page()

    def decimal_text(value, places):
        return f'{Decimal(value):.{places}f}'

    def heading(label):
        nonlocal y
        ensure_space(36)
        _pdf_text(commands, 52, y, label, 13, 'F2', '#124e2c')
        _pdf_line(commands, 52, y - 8, 560, y - 8, '#29b973', 1.2)
        y -= 30

    def key_value(x, label, value, width=230):
        nonlocal y
        _pdf_text(commands, x, y, label.upper(), 7, 'F2', '#6b7280')
        for index, line in enumerate(_wrap_pdf_lines(value, max(24, int(width / 5.8)))[:3]):
            _pdf_text(commands, x, y - 13 - (index * 11), line, 10, 'F1', '#111111')

    def card(x, y_top, width, height, label, value, accent='#29b973'):
        _pdf_rect(commands, x, y_top - height, width, height, fill='#f7faf8', stroke='#d2d0d2', stroke_width=0.5)
        _pdf_rect(commands, x, y_top - height, 5, height, fill=accent)
        _pdf_text(commands, x + 14, y_top - 18, label.upper(), 7, 'F2', '#6b7280')
        for index, line in enumerate(_wrap_pdf_lines(value, max(18, int((width - 24) / 5.8)))[:2]):
            _pdf_text(commands, x + 14, y_top - 34 - (index * 12), line, 11, 'F2' if index == 0 else 'F1', '#111111')

    def paragraph(text, max_chars=92, size=9, color='#374151', font='F1'):
        nonlocal y
        for line in _wrap_pdf_lines(text, max_chars):
            ensure_space(16)
            _pdf_text(commands, 52, y, line, size, font, color)
            y -= 13

    start_page()

    _pdf_text(commands, 52, y, f'Trade #{trade.id}', 28, 'F2', '#124e2c')
    _pdf_text(commands, 52, y - 22, f'Offer #{trade.offer_id} | Generated {generated_at}', 10, 'F1', '#6b7280')
    _pdf_text(commands, 52, y - 38, f'Requested by {requested_by.username if requested_by else "Unknown"}', 9, 'F1', '#6b7280')
    y -= 68

    card(52, y, 158, 62, 'You can verify', 'Parties, terms, TXIDs, messages', '#29b973')
    card(226, y, 158, 62, 'Status', f'{trade.status} / {trade.milestone}', status_color)
    card(400, y, 160, 62, 'Total', f'{total_btc:.12f} BTC', '#15689e')
    y -= 92

    heading('Trade Summary')
    key_value(52, 'Alice - HNS seller', alice_user.username)
    key_value(315, 'Bob - HNS buyer / BTC seller', bob_user.username)
    y -= 52
    key_value(52, 'Amount', f'{decimal_text(amount_hns, 8)} HNS')
    key_value(185, 'Price', f'{decimal_text(price_btc, 12)} BTC/HNS')
    key_value(365, 'Payment method', trade.offer.payment_method)
    y -= 52
    key_value(52, 'Created', created_at)
    key_value(215, 'Updated', updated_at)
    key_value(378, 'Gems bond', f'{trade.maker_bond_amount or 0} / {trade.maker_bond_status}')
    y -= 38

    if trade.latest_note:
        heading('Latest Room Note')
        paragraph(trade.latest_note, 96, 9, '#374151')
        y -= 8

    heading('Transaction Records')
    key_value(52, 'Alice lock TXID', trade.alice_lock_txid or 'Not submitted', 500)
    y -= 48
    key_value(52, 'Bob lock TXID', trade.bob_lock_txid or 'Not submitted', 500)
    y -= 48

    heading('Review State')
    key_value(52, 'Admin review', trade.admin_review_status)
    key_value(220, 'Admin resolution', trade.admin_resolution or 'Pending')
    if trade.admin_notes:
        y -= 44
        paragraph(f'Admin notes: {trade.admin_notes}', 96, 9, '#374151')
    y -= 34

    if trade.offer.notes:
        ensure_space(80)
        heading('Offer Notes')
        paragraph(trade.offer.notes, 96, 9, '#374151')
        y -= 8

    heading('Message Transcript')
    messages = sorted(trade.messages, key=lambda message: message.created_at)
    if not messages:
        paragraph('No messages recorded in this trade room yet.', 96, 9, '#6b7280')
    else:
        for message in messages:
            when = message.created_at.strftime('%Y-%m-%d %H:%M UTC') if message.created_at else 'Unknown time'
            lines = _wrap_pdf_lines(message.message, 84)
            box_height = 34 + (len(lines) * 12)
            ensure_space(box_height + 10)
            _pdf_rect(commands, 52, y - box_height, 508, box_height, fill='#f7faf8', stroke='#d2d0d2', stroke_width=0.5)
            _pdf_text(commands, 66, y - 18, message.user.username, 10, 'F2', '#124e2c')
            _pdf_text(commands, 380, y - 18, when, 8, 'F1', '#6b7280')
            line_y = y - 36
            for line in lines:
                _pdf_text(commands, 66, line_y, line, 9, 'F1', '#374151')
                line_y -= 12
            y -= box_height + 12

    ensure_space(80)
    heading('Counterparty Feedback')
    feedback_items = sorted(trade.feedback, key=lambda item: item.created_at)
    if not feedback_items:
        paragraph('No feedback recorded yet.', 96, 9, '#6b7280')
    else:
        for feedback in feedback_items:
            when = feedback.created_at.strftime('%Y-%m-%d %H:%M UTC') if feedback.created_at else 'Unknown time'
            lines = _wrap_pdf_lines(feedback.comment or 'No note left.', 84)
            box_height = 34 + (len(lines) * 12)
            ensure_space(box_height + 10)
            _pdf_rect(commands, 52, y - box_height, 508, box_height, fill='#f7faf8', stroke='#d2d0d2', stroke_width=0.5)
            _pdf_text(commands, 66, y - 18, f'{feedback.rating}/5 from {feedback.reviewer.username}', 10, 'F2', '#124e2c')
            _pdf_text(commands, 360, y - 18, f'for {feedback.reviewee.username} | {when}', 8, 'F1', '#6b7280')
            line_y = y - 36
            for line in lines:
                _pdf_text(commands, 66, line_y, line, 9, 'F1', '#374151')
                line_y -= 12
            y -= box_height + 12

    ensure_space(66)
    heading('Record Note')
    paragraph(
        'This receipt is generated from the Liquidity.spot trade room record. Participants remain responsible for verifying counterparties, transaction IDs, confirmations, and settlement.',
        96,
        8,
        '#6b7280'
    )

    start_page()
    return _build_pdf_document(f'Liquidity.spot Trade #{trade.id} Receipt', pages)

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
            'maker_mode': _external_url_for('main.maker_mode'),
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


@main_bp.route('/gems')
def gems_guide():
    return render_template('gems.html')


@main_bp.route('/p2p')
def p2p():
    offers = P2POffer.query.filter_by(status='open').order_by(P2POffer.created_at.desc()).all()
    my_trades = []
    current_price = _current_hns_btc_price()

    if session.get('user_id'):
        my_trades = P2PTrade.query.filter(
            (P2PTrade.creator_id == session['user_id']) |
            (P2PTrade.counterparty_id == session['user_id'])
        ).order_by(P2PTrade.updated_at.desc()).all()

    offer_contexts = {offer.id: _p2p_offer_context(offer) for offer in offers}
    return render_template(
        'p2p.html',
        offers=offers,
        offer_contexts=offer_contexts,
        my_trades=my_trades,
        current_price=current_price,
    )


@main_bp.route('/p2p/offers/<int:offer_id>')
def p2p_offer_details(offer_id):
    offer = P2POffer.query.get_or_404(offer_id)
    trade = offer.trade[0] if offer.trade else None
    return render_template(
        'p2p_offer.html',
        offer=offer,
        offer_context=_p2p_offer_context(offer),
        trade=trade,
        is_creator=session.get('user_id') == offer.creator_id,
        is_participant=bool(
            trade and session.get('user_id') in [trade.creator_id, trade.counterparty_id]
        ),
        share_url=_external_url_for('main.p2p_offer_details', offer_id=offer.id),
    )


@main_bp.route('/maker-mode')
def maker_mode():
    current_price = _current_hns_btc_price()
    open_p2p_offers = P2POffer.query.filter_by(status='open').count()
    open_atomic_orders = Order.query.filter_by(status='open').count()
    active_p2p_trades = P2PTrade.query.filter(
        P2PTrade.status.notin_(['completed', 'canceled', 'disputed', 'no_show'])
    ).count()

    return render_template(
        'maker_mode.html',
        current_price=current_price,
        open_p2p_offers=open_p2p_offers,
        open_atomic_orders=open_atomic_orders,
        active_p2p_trades=active_p2p_trades,
    )

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
        offer_context = _p2p_offer_context(offer)
        flash(
            f"Offer #{offer.id} is live. {offer_context['waiting_label']}. "
            'Share this page with someone who can take the other side.',
            'success'
        )
        return redirect(url_for('main.p2p_offer_details', offer_id=offer.id))
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
            flash({
                'title': 'This offer cannot start yet',
                'detail': (
                    f'The offer creator does not have enough Gems to cover their '
                    f'{offer.gems_stake}-Gem bond. No Gems, HNS, or BTC were taken from you.'
                ),
                'tips': [
                    'Ask the offer creator to add Gems in GFAVIP, then try again.',
                    'Ask them to cancel and repost the offer with a smaller bond or no bond.',
                    'Choose another open offer while this one is unavailable.',
                ],
            }, 'gems_error')
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
    alice_user, bob_user = _p2p_trade_parties(trade)
    current_role = 'Alice' if session['user_id'] == alice_user.id else 'Bob'
    counterparty_user = _p2p_counterparty(trade, session['user_id'])
    my_feedback = P2PTradeFeedback.query.filter_by(
        trade_id=trade.id,
        reviewer_id=session['user_id']
    ).first()
    trade_feedback = P2PTradeFeedback.query.filter_by(trade_id=trade.id).order_by(
        P2PTradeFeedback.created_at.asc()
    ).all()

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
        current_role=current_role,
        my_feedback=my_feedback,
        trade_feedback=trade_feedback,
        counterparty_feedback_stats=_p2p_feedback_stats(counterparty_user.id)
    )


@main_bp.route('/p2p/trades/<int:trade_id>/feedback', methods=['POST'])
@login_required
def submit_p2p_trade_feedback(trade_id):
    trade = P2PTrade.query.get_or_404(trade_id)
    user_id = session['user_id']

    if user_id not in [trade.creator_id, trade.counterparty_id]:
        flash('You do not have permission to rate this trade.', 'error')
        return redirect(url_for('main.p2p'))

    if trade.status != 'completed':
        flash('Feedback opens after the trade is marked complete.', 'warning')
        return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))

    counterparty = _p2p_counterparty(trade, user_id)
    try:
        rating = int(request.form.get('rating') or 0)
    except ValueError:
        rating = 0

    if rating < 1 or rating > 5:
        flash('Choose a rating from 1 to 5.', 'error')
        return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))

    comment = (request.form.get('comment') or '').strip()[:1000] or None
    feedback = P2PTradeFeedback.query.filter_by(
        trade_id=trade.id,
        reviewer_id=user_id
    ).first()

    if feedback:
        feedback.rating = rating
        feedback.comment = comment
        feedback.reviewee_id = counterparty.id
        flash('Feedback updated. Thanks for keeping the reputation trail current.', 'success')
    else:
        feedback = P2PTradeFeedback(
            trade_id=trade.id,
            reviewer_id=user_id,
            reviewee_id=counterparty.id,
            rating=rating,
            comment=comment
        )
        db.session.add(feedback)
        flash('Feedback saved. This now counts toward the counterparty reputation trail.', 'success')

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Error saving P2P feedback')
        flash('Could not save feedback. Please try again.', 'error')

    return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))


@main_bp.route('/p2p/trades/<int:trade_id>/receipt.<file_format>')
@login_required
def p2p_trade_receipt(trade_id, file_format):
    trade = P2PTrade.query.get_or_404(trade_id)

    if session['user_id'] not in [trade.creator_id, trade.counterparty_id]:
        flash('You do not have permission to download this trade receipt.', 'error')
        return redirect(url_for('main.p2p'))

    requested_by = User.query.get(session['user_id'])
    receipt_text = _build_p2p_trade_receipt_text(trade, requested_by=requested_by)
    filename_base = f'liquidity-spot-trade-{trade.id}-receipt'

    if file_format == 'txt':
        return Response(
            receipt_text,
            mimetype='text/plain; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{filename_base}.txt"'}
        )

    if file_format == 'pdf':
        pdf_bytes = _build_p2p_trade_receipt_pdf(trade, requested_by=requested_by)
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename="{filename_base}.pdf"'}
        )

    flash('Unsupported receipt format.', 'error')
    return redirect(url_for('main.p2p_trade_room', trade_id=trade.id))


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
        flash('Trade completed. You can now leave counterparty feedback in the trade room.', 'success')

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

@main_bp.route('/opensource')
def opensource():
    return render_template('opensource.html')

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
    my_feedback = P2PTradeFeedback.query.filter_by(reviewer_id=user.id).all()
    feedback_by_trade_id = {item.trade_id: item for item in my_feedback}

    active_swaps = [swap for swap in my_swaps if swap.status not in ['completed', 'canceled', 'refunded']]
    final_p2p_statuses = {'completed', 'canceled', 'disputed', 'no_show'}
    active_p2p_trades = [trade for trade in my_p2p_trades if trade.status not in final_p2p_statuses]
    historical_p2p_trades = [trade for trade in my_p2p_trades if trade.status in final_p2p_statuses]
    feedback_needed_trades = [
        trade for trade in historical_p2p_trades
        if trade.status == 'completed' and trade.id not in feedback_by_trade_id
    ]
    historical_swaps = [swap for swap in my_swaps if swap.status in ['completed', 'canceled', 'refunded', 'disputed']]

    return render_template(
        'dashboard.html',
        user=user,
        orders=my_orders,
        swaps=my_swaps,
        active_swaps=active_swaps,
        active_p2p_trades=active_p2p_trades,
        historical_p2p_trades=historical_p2p_trades,
        historical_swaps=historical_swaps,
        feedback_needed_trades=feedback_needed_trades,
        feedback_by_trade_id=feedback_by_trade_id
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
    feedback_stats = _p2p_feedback_stats(user.id)
    recent_feedback = P2PTradeFeedback.query.filter_by(reviewee_id=user.id).order_by(
        P2PTradeFeedback.created_at.desc()
    ).limit(5).all()

    return render_template(
        'profile.html',
        user=user,
        p2p_offer_count=p2p_offer_count,
        p2p_trade_count=p2p_trade_count,
        order_count=order_count,
        feedback_stats=feedback_stats,
        recent_feedback=recent_feedback
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
    current_price = _current_hns_btc_price()
        
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
            if swap.status != 'initiated':
                flash('Alice has already recorded the HNS lock. The next step belongs to Bob.', 'error')
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
                if txid in [swap.alice_lock_txid, swap.bob_lock_txid, swap.alice_claim_txid, swap.bob_claim_txid]:
                    flash('Refund TXID must be a new refund transaction, not an already-recorded lock or claim TXID.', 'error')
                    return redirect(url_for('main.swap_details', id=swap.id))
                swap.alice_refund_txid = txid
                swap.latest_note = note or 'Alice posted an HNS refund transaction.'
            else:
                txid = _clean_txid(request.form.get('bob_refund_txid'))
                if not txid:
                    flash('Enter a valid 64-character BTC refund TXID.', 'error')
                    return redirect(url_for('main.swap_details', id=swap.id))
                if txid in [swap.alice_lock_txid, swap.bob_lock_txid, swap.alice_claim_txid, swap.bob_claim_txid]:
                    flash('Refund TXID must be a new refund transaction, not an already-recorded lock or claim TXID.', 'error')
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
