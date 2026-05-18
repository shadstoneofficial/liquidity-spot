from flask import Blueprint, redirect, request, session, url_for, current_app, flash, render_template
from functools import wraps
import requests
from models import db, User
from datetime import datetime
import hashlib
import hmac
import re
import secrets

auth_bp = Blueprint('auth', __name__)
GUEST_RECOVERY_TOKEN_PATTERN = re.compile(r'^ls-guest-[0-9a-f]{4}(?:-[0-9a-f]{4}){7}$')


def generate_guest_recovery_token():
    token_hex = secrets.token_hex(16)
    groups = '-'.join(token_hex[index:index + 4] for index in range(0, len(token_hex), 4))
    return f'ls-guest-{groups}'


def normalize_guest_recovery_token(raw_token):
    token = re.sub(r'\s+', '', (raw_token or '').strip().lower())
    return token if GUEST_RECOVERY_TOKEN_PATTERN.fullmatch(token) else None


def guest_recovery_digest(raw_token):
    token = normalize_guest_recovery_token(raw_token)
    if not token:
        return None
    pepper = current_app.config.get('SECRET_KEY') or 'liquidity-spot-dev'
    return hmac.new(pepper.encode('utf-8'), token.encode('utf-8'), hashlib.sha256).hexdigest()


def attach_guest_recovery_token(user):
    token = generate_guest_recovery_token()
    user.guest_recovery_digest = guest_recovery_digest(token)
    session['guest_recovery_token'] = token
    return token

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
    if 'user_id' in session and session.get('auth_method') != 'guest':
        return redirect(url_for('main.dashboard'))

    return render_template('login.html')


@auth_bp.route('/guest/recovery', methods=['GET', 'POST'])
def guest_recovery():
    if request.method == 'POST':
        token = normalize_guest_recovery_token(request.form.get('guest_recovery_token'))
        digest = guest_recovery_digest(token)
        if not digest:
            flash('Enter a valid guest recovery key.', 'error')
            return redirect(url_for('auth.login'))

        user = User.query.filter_by(guest_recovery_digest=digest, tier='guest').first()
        if not user:
            flash('No guest account matched that recovery key.', 'error')
            return redirect(url_for('auth.login'))

        session.clear()
        session['user_id'] = user.id
        session['username'] = user.username
        session['tier'] = user.tier
        session['auth_method'] = 'guest'
        session['guest_recovery_token'] = token
        flash(f'Recovered {user.username}. You can keep trading under this anonymous guest identity.', 'success')
        return redirect(url_for('main.dashboard'))

    if session.get('auth_method') != 'guest' or not session.get('user_id'):
        flash('Start or recover a guest session first.', 'warning')
        return redirect(url_for('auth.login'))

    user = User.query.get(session['user_id'])
    if not user or user.tier != 'guest':
        flash('Guest recovery keys are only available for guest accounts.', 'warning')
        return redirect(url_for('main.dashboard'))

    token = session.get('guest_recovery_token')
    if not user.guest_recovery_digest or not guest_recovery_digest(token) == user.guest_recovery_digest:
        token = attach_guest_recovery_token(user)
        db.session.commit()

    return render_template('guest_recovery.html', token=token, user=user)


@auth_bp.route('/guest/recovery/rotate', methods=['POST'])
@login_required
def rotate_guest_recovery():
    if session.get('auth_method') != 'guest':
        flash('Only guest accounts use guest recovery keys.', 'warning')
        return redirect(url_for('main.dashboard'))

    user = User.query.get_or_404(session['user_id'])
    token = attach_guest_recovery_token(user)
    db.session.commit()
    flash('Guest recovery key rotated. The old key no longer works.', 'success')
    return render_template('guest_recovery.html', token=token, user=user)


@auth_bp.route('/login/gfavip')
def login_gfavip():
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
    session['auth_method'] = 'gfavip'
    
    flash('Logged in successfully!', 'success')
    return redirect(url_for('main.dashboard'))

@auth_bp.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('main.index'))
