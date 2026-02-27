from flask import Blueprint, render_template, request, flash, redirect, url_for, session
from routes.auth import login_required
from functools import wraps
import requests

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
    return render_template('admin.html')

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
