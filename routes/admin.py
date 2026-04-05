from flask import Blueprint, render_template, request, flash, redirect, url_for, session
from routes.auth import login_required
from functools import wraps
import requests
from models import P2PTrade, db

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
    return render_template(
        'admin.html',
        recent_p2p_trades=recent_p2p_trades,
        flagged_p2p_trades=flagged_p2p_trades
    )

@admin_bp.route('/admin/credit', methods=['POST'])
@login_required
@admin_required
def credit_gems():
    user_id = request.form.get('user_id')
    amount = request.form.get('amount')
    reason = request.form.get('reason')
    
    token = session.get('token')
    
    try:
        response = requests.post(
            "https://wallet.gfavip.com/api/wallet/credit",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "userId": user_id,
                "amount": int(amount),
                "reason": reason,
                "metadata": {
                    "source": "liquidity-spot-admin"
                }
            }
        )
        
        if response.status_code == 200:
            data = response.json()
            flash(f"Successfully credited {data.get('amountCredited')} gems to {data.get('userId')}", 'success')
        else:
            flash(f"Error: {response.text}", 'error')
            
    except Exception as e:
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

    trade.admin_review_status = admin_review_status
    trade.admin_resolution = admin_resolution
    trade.status = status
    if admin_notes is not None:
        trade.admin_notes = admin_notes

    db.session.commit()
    flash(f'P2P trade #{trade.id} admin review updated.', 'success')
    return redirect(url_for('admin.credit'))
