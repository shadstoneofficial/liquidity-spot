from flask import Blueprint, redirect, request, session, url_for, current_app, flash
from functools import wraps
import requests
from models import db, User
from datetime import datetime

auth_bp = Blueprint('auth', __name__)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in first.', 'warning')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

@auth_bp.route('/login')
def login():
    service_name = current_app.config.get('GFAVIP_SERVICE_NAME', 'liquidity-spot')
    redirect_uri = current_app.config.get('REDIRECT_URI', 'http://localhost:8000/callback')
    sso_url = f"https://wallet.gfavip.com/api/auth/sso/authorize?redirect_uri={redirect_uri}&service={service_name}"
    return redirect(sso_url)

@auth_bp.route('/callback')
def callback():
    user_id = request.args.get('user_id')
    email = request.args.get('email')
    username = request.args.get('username')
    token = request.args.get('token')
    tier = request.args.get('tier')
    credits = request.args.get('credits')

    if not all([user_id, token]):
        flash('Authentication failed: Missing required parameters.', 'error')
        return redirect(url_for('main.index'))

    # Validate token
    validate_url = "https://wallet.gfavip.com/api/auth/validate"
    headers = {'Authorization': f'Bearer {token}'}
    
    try:
        response = requests.get(validate_url, headers=headers)
        if response.status_code != 200:
            flash('Authentication failed: Invalid token.', 'error')
            return redirect(url_for('main.index'))
    except requests.RequestException:
        flash('Authentication failed: Validation service unavailable.', 'error')
        return redirect(url_for('main.index'))

    # Create or Update User
    user = User.query.get(user_id)
    if not user:
        user = User(id=user_id)
        db.session.add(user)
    
    user.username = username
    user.email = email
    user.tier = tier
    user.gems_balance = int(credits) if credits else 0
    user.last_sync = datetime.utcnow()
    
    db.session.commit()

    # Set Session
    session['user_id'] = user_id
    session['username'] = username
    session['tier'] = tier
    session['token'] = token
    
    flash('Logged in successfully!', 'success')
    return redirect(url_for('main.dashboard'))

@auth_bp.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('main.index'))
