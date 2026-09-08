from flask import Blueprint, render_template, request, flash, redirect, url_for, session
from routes.auth import login_required
from functools import wraps
from datetime import datetime
from models import P2PTrade, Swap, SwapMessage, User, db
from services.gems_service import GemsServiceError, credit_gems as wallet_credit_gems, is_wallet_service_configured

admin_bp = Blueprint('admin', __name__)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get('tier') != 'team':
            flash('Access denied. Admin only.', 'error')
            return redirect(url_for('main.dashboard'))
        return f(*args, **kwargs)
    return decorated_function

@admin_bp.route('/admin/credit', methods=['GET'])
@login_required
@admin_required
def credit():
    recent_p2p_trades = P2PTrade.query.order_by(P2PTrade.updated_at.desc()).limit(20).all()
    flagged_p2p_trades = P2PTrade.query.filter(
        (P2PTrade.status.in_(['disputed', 'no_show', 'canceled'])) |
        (P2PTrade.admin_review_status != 'resolved')
    ).order_by(P2PTrade.updated_at.desc()).all()
    recent_swaps = Swap.query.order_by(Swap.updated_at.desc()).limit(20).all()
    flagged_swaps = Swap.query.filter(
        (Swap.status == 'disputed') |
        (Swap.admin_review_status == 'in_review')
    ).order_by(Swap.updated_at.desc()).all()
    return render_template(
        'admin.html',
        recent_p2p_trades=recent_p2p_trades,
        flagged_p2p_trades=flagged_p2p_trades,
        recent_swaps=recent_swaps,
        flagged_swaps=flagged_swaps
    )

@admin_bp.route('/admin/credit', methods=['POST'])
@login_required
@admin_required
def credit_gems():
    user_id = request.form.get('user_id')
    amount = request.form.get('amount')
    reason = request.form.get('reason')

    if not is_wallet_service_configured():
        flash('Wallet Service API is not configured.', 'error')
        return redirect(url_for('admin.credit'))

    try:
        data = wallet_credit_gems(
            user_id,
            int(amount),
            reason,
            metadata={
                'service': 'liquidity-spot',
                'operation_type': 'admin_manual_credit'
            }
        )
        flash(f"Successfully credited {data.get('amountCredited')} gems to {data.get('userId')}", 'success')
    except (ValueError, GemsServiceError) as e:
        flash(f"Request failed: {str(e)}", 'error')
        
    return redirect(url_for('admin.credit'))

@admin_bp.route('/admin/p2p-trades/<int:trade_id>/resolve', methods=['POST'])
@login_required
@admin_required
def resolve_p2p_trade(trade_id):
    trade = P2PTrade.query.get_or_404(trade_id)
    admin_review_status = request.form.get('admin_review_status') or trade.admin_review_status
    admin_resolution = request.form.get('admin_resolution') or trade.admin_resolution
    admin_notes = request.form.get('admin_notes')
    status = request.form.get('status') or trade.status
    bond_action = request.form.get('bond_action') or 'none'

    trade.admin_review_status = admin_review_status
    trade.admin_resolution = admin_resolution
    trade.status = status
    if admin_notes is not None:
        trade.admin_notes = admin_notes

    if bond_action == 'refund_full' and trade.maker_bond_status == 'locked' and trade.maker_bond_amount > 0:
        if not is_wallet_service_configured():
            flash('Wallet Service API is not configured, so the maker bond cannot be refunded yet.', 'error')
            return redirect(url_for('admin.credit'))

        try:
            wallet_credit_gems(
                trade.creator_id,
                trade.maker_bond_amount,
                f'Liquidity.spot admin refund for maker bond on P2P trade #{trade.id}',
                metadata={
                    'service': 'liquidity-spot',
                    'trade_id': trade.id,
                    'offer_id': trade.offer_id,
                    'operation_type': 'maker_bond_refund_admin',
                    'resolution_type': admin_resolution or status
                }
            )
            trade.maker_bond_status = 'refunded'
            trade.maker_bond_released_at = datetime.utcnow()
            trade.maker_bond_resolution = 'refunded'
            trade.maker_bond_error = None
            trade.offer.maker_bond_status = 'refunded'
            trade.offer.maker_bond_released_at = trade.maker_bond_released_at
            trade.offer.maker_bond_resolution = 'refunded'
            trade.offer.maker_bond_error = None
        except GemsServiceError as exc:
            trade.maker_bond_error = str(exc)
            flash(f'Could not refund maker bond: {exc}', 'error')
            return redirect(url_for('admin.credit'))

    if bond_action == 'slash_full' and trade.maker_bond_status == 'locked' and trade.maker_bond_amount > 0:
        trade.maker_bond_status = 'slashed'
        trade.maker_bond_released_at = datetime.utcnow()
        trade.maker_bond_resolution = 'slashed_full'
        trade.maker_bond_error = None
        trade.offer.maker_bond_status = 'slashed'
        trade.offer.maker_bond_released_at = trade.maker_bond_released_at
        trade.offer.maker_bond_resolution = 'slashed_full'
        trade.offer.maker_bond_error = None

    db.session.commit()
    flash(f'P2P trade #{trade.id} admin review updated.', 'success')
    return redirect(url_for('admin.credit'))


@admin_bp.route('/admin/swaps/<int:swap_id>/resolve', methods=['POST'])
@login_required
@admin_required
def resolve_swap(swap_id):
    swap = Swap.query.get_or_404(swap_id)
    admin_review_status = request.form.get('admin_review_status') or swap.admin_review_status
    admin_resolution = request.form.get('admin_resolution') or swap.admin_resolution
    admin_notes = request.form.get('admin_notes')
    status = request.form.get('status') or swap.status
    reputation_action = request.form.get('reputation_action') or 'none'

    swap.admin_review_status = admin_review_status
    swap.admin_resolution = admin_resolution
    swap.status = status
    if admin_notes is not None:
        swap.admin_notes = admin_notes
    if status in ['completed', 'canceled', 'refunded']:
        swap.completed_at = swap.completed_at or datetime.utcnow()
    swap.latest_note = admin_notes or f'Admin review updated: {admin_review_status} / {admin_resolution or status}.'

    if reputation_action in ['alice_no_show', 'bob_no_show']:
        actor_id = swap.role_alice_user_id
        if reputation_action == 'bob_no_show':
            actor_id = next(
                (user_id for user_id in {swap.order.user_id, swap.matcher_id} if user_id != swap.role_alice_user_id),
                None
            )
        if actor_id:
            user = User.query.get(actor_id)
            if user:
                user.stale_no_shows = (user.stale_no_shows or 0) + 1
                user.disputed_swaps = (user.disputed_swaps or 0) + 1

    if admin_notes:
        db.session.add(SwapMessage(
            swap_id=swap.id,
            user_id=session['user_id'],
            message=f'Admin note: {admin_notes}'
        ))

    db.session.commit()
    flash(f'Atomic swap #{swap.id} admin review updated.', 'success')
    return redirect(url_for('admin.credit'))
