from requests import RequestException
from flask import current_app
from services.http_client import post as http_post


class GemsServiceError(Exception):
    pass


def is_wallet_service_configured():
    return bool(
        current_app.config.get('GFAVIP_WALLET_API_KEY') and
        current_app.config.get('GFAVIP_WALLET_BASE_URL')
    )


def _wallet_headers():
    api_key = current_app.config.get('GFAVIP_WALLET_API_KEY')
    if not api_key:
        raise GemsServiceError('GFAVIP wallet API key is not configured.')

    return {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }


def _wallet_url(path):
    base_url = (current_app.config.get('GFAVIP_WALLET_BASE_URL') or '').rstrip('/')
    if not base_url:
        raise GemsServiceError('GFAVIP wallet base URL is not configured.')
    return f'{base_url}{path}'


def _wallet_post(path, user_id, amount, reason, metadata=None):
    try:
        response = http_post(
            _wallet_url(path),
            headers=_wallet_headers(),
            json={
                'userId': user_id,
                'amount': int(amount),
                'reason': reason,
                'metadata': metadata or {}
            },
            timeout=15
        )
    except RequestException as exc:
        raise GemsServiceError(f'Wallet service request failed: {exc}') from exc

    try:
        data = response.json()
    except ValueError:
        data = {'message': response.text}

    if not response.ok:
        raise GemsServiceError(data.get('message', 'Wallet service request failed.'))

    return data


def credit_gems(user_id, amount, reason, metadata=None):
    return _wallet_post('/api/external/wallet/credit', user_id, amount, reason, metadata)


def deduct_gems(user_id, amount, reason, metadata=None):
    return _wallet_post('/api/external/wallet/deduct', user_id, amount, reason, metadata)
