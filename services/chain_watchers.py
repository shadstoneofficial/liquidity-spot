import re
import hashlib
from datetime import datetime

import requests


HEX_RE = re.compile(r'^[0-9a-fA-F]+$')


class WatcherError(Exception):
    pass


def _request_json(base_url, path, timeout=10):
    if not base_url:
        raise WatcherError('Watcher base URL is not configured.')

    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise WatcherError(str(exc)) from exc

    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise WatcherError(f'Watcher returned HTTP {response.status_code}.')

    try:
        return response.json()
    except ValueError as exc:
        raise WatcherError('Watcher returned invalid JSON.') from exc


def _walk_hex_strings(value):
    if isinstance(value, str):
        if len(value) >= 32 and len(value) % 2 == 0 and HEX_RE.fullmatch(value):
            yield value.lower()
        return
    if isinstance(value, list):
        for item in value:
            yield from _walk_hex_strings(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_hex_strings(item)


def extract_matching_secret(tx_json, secret_hash):
    """Find a hex preimage in witness/script fields that hashes to secret_hash."""
    for candidate in _walk_hex_strings(tx_json):
        if len(candidate) not in (32, 64, 128):
            continue
        try:
            if hashlib.sha256(bytes.fromhex(candidate)).hexdigest() == secret_hash:
                return candidate
        except ValueError:
            continue
    return None


def verify_bitcoin_tx(txid, base_url, secret_hash=None, require_secret=False):
    tx_json = _request_json(base_url, f'tx/{txid}')
    if not tx_json:
        return {
            'found': False,
            'verified_at': None,
            'secret': None,
            'raw': None,
        }

    secret = extract_matching_secret(tx_json, secret_hash) if secret_hash else None
    if require_secret and not secret:
        return {
            'found': True,
            'verified_at': None,
            'secret': None,
            'raw': tx_json,
            'error': 'Transaction found, but no matching secret was found in witness/script data.',
        }

    return {
        'found': True,
        'verified_at': datetime.utcnow(),
        'secret': secret,
        'raw': tx_json,
    }


def verify_hns_tx(txid, base_url):
    tx_json = _request_json(base_url, f'tx/{txid}')
    if not tx_json:
        return {
            'found': False,
            'verified_at': None,
            'raw': None,
        }

    return {
        'found': True,
        'verified_at': datetime.utcnow(),
        'raw': tx_json,
    }
