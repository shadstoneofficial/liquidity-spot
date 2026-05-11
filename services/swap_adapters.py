from decimal import Decimal


SATOSHIS_PER_BTC = Decimal('100000000')
HNS_DOLLARY_PER_COIN = Decimal('1000000')


def _decimal_string(value):
    return format(Decimal(value), 'f')


def _btc_sats(value):
    return int((Decimal(value) * SATOSHIS_PER_BTC).to_integral_value())


def _hns_dollary(value):
    return int((Decimal(value) * HNS_DOLLARY_PER_COIN).to_integral_value())


def _swap_amounts(swap):
    hns = Decimal(swap.order.amount_hns)
    btc = hns * Decimal(swap.order.price_btc_per_hns)
    return {
        'hns': _decimal_string(hns),
        'hns_dollary': _hns_dollary(hns),
        'btc': _decimal_string(btc),
        'btc_sats': _btc_sats(btc),
    }


def _participants(swap):
    alice_id = swap.role_alice_user_id
    maker_id = swap.order.user_id
    bob_id = swap.matcher_id if maker_id == alice_id else maker_id
    return {
        'alice_user_id': alice_id,
        'bob_user_id': bob_id,
        'maker_user_id': maker_id,
        'matcher_user_id': swap.matcher_id,
    }


def build_bob_hns_intent(swap, app_base_url):
    """Machine-readable request for a Bob Wallet HNS HTLC helper.

    Liquidity.spot never receives seed phrases or wallet passwords. This intent is
    designed for a local wallet adapter to display, sign, broadcast, then return a
    TXID to the swap progress endpoint.
    """
    amounts = _swap_amounts(swap)
    participants = _participants(swap)
    return {
        'adapter': 'bob-wallet',
        'version': 1,
        'network': 'hns',
        'chain': 'handshake',
        'swap_id': swap.id,
        'role': 'alice',
        'action': 'create_hns_htlc_lock',
        'amount_hns': amounts['hns'],
        'amount_dollary': amounts['hns_dollary'],
        'secret_hash_sha256': swap.secret_hash,
        'timelock_seconds': swap.timelock_alice_sec,
        'timelock_blocks_estimate': swap.timelock_alice_sec // 600,
        'refund_locktime': swap.hns_refund_locktime,
        'claim_public_key': swap.hns_claim_public_key,
        'refund_public_key': swap.hns_refund_public_key,
        'htlc_address': swap.hns_lock_address,
        'htlc_script': swap.hns_lock_script,
        'htlc_value': swap.hns_lock_value,
        'alice_user_id': participants['alice_user_id'],
        'bob_user_id': participants['bob_user_id'],
        'metadata_callback': {
            'method': 'POST',
            'url': f'{app_base_url}/api/swaps/{swap.id}/hns-htlc?token={swap.adapter_token}',
            'fields': [
                'hns_claim_public_key',
                'hns_refund_public_key',
                'hns_refund_locktime',
                'hns_lock_address',
                'hns_lock_script',
                'hns_lock_value',
                'hns_lock_output_index',
            ],
        },
        'callback': {
            'method': 'POST',
            'url': f'{app_base_url}/api/swaps/{swap.id}/txids/alice-lock?token={swap.adapter_token}',
            'fields': ['txid', 'hns_lock_output_index', 'hns_lock_address', 'hns_lock_script', 'hns_lock_value'],
        },
        'safety': {
            'custody': 'non-custodial',
            'signing': 'local-wallet-only',
            'liquidity_spot_receives_private_keys': False,
        },
    }


def build_bitcoin_htlc_script_template(swap):
    """Return a standard SHA256-preimage HTLC script template.

    The concrete public keys and refund locktime must be supplied by a Bitcoin
    wallet/PSBT helper because Liquidity.spot does not custody or derive keys.
    """
    return (
        'OP_IF '
        'OP_SHA256 <secret_hash_sha256> OP_EQUALVERIFY <alice_btc_pubkey> OP_CHECKSIG '
        'OP_ELSE '
        '<refund_locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP <bob_btc_pubkey> OP_CHECKSIG '
        'OP_ENDIF'
    )


def build_bitcoin_psbt_intent(swap, app_base_url):
    amounts = _swap_amounts(swap)
    participants = _participants(swap)
    return {
        'adapter': 'bitcoin-psbt',
        'version': 1,
        'network': 'btc',
        'chain': 'bitcoin',
        'swap_id': swap.id,
        'role': 'bob',
        'action': 'fund_btc_htlc_psbt',
        'amount_btc': amounts['btc'],
        'amount_sats': amounts['btc_sats'],
        'secret_hash_sha256': swap.secret_hash,
        'timelock_seconds': swap.timelock_bob_sec,
        'timelock_blocks_estimate': swap.timelock_bob_sec // 600,
        'script_template': build_bitcoin_htlc_script_template(swap),
        'required_wallet_inputs': [
            'bob_btc_refund_pubkey',
            'alice_btc_claim_pubkey',
            'refund_locktime',
            'funding_utxos',
            'change_address',
        ],
        'alice_user_id': participants['alice_user_id'],
        'bob_user_id': participants['bob_user_id'],
        'callback': {
            'method': 'POST',
            'url': f'{app_base_url}/api/swaps/{swap.id}/txids/bob-lock?token={swap.adapter_token}',
            'field': 'txid',
        },
        'safety': {
            'custody': 'non-custodial',
            'signing': 'local-wallet-only',
            'liquidity_spot_receives_private_keys': False,
        },
    }


def build_claim_intents(swap, app_base_url):
    amounts = _swap_amounts(swap)
    return {
        'alice_btc_claim': {
            'adapter': 'bitcoin-psbt',
            'version': 1,
            'network': 'btc',
            'chain': 'bitcoin',
            'swap_id': swap.id,
            'role': 'alice',
            'action': 'claim_btc_htlc_with_secret',
            'amount_btc': amounts['btc'],
            'amount_sats': amounts['btc_sats'],
            'secret_hash_sha256': swap.secret_hash,
            'requires_secret': True,
            'bob_lock_txid': swap.bob_lock_txid,
            'callback': {
                'method': 'POST',
                'url': f'{app_base_url}/api/swaps/{swap.id}/txids/alice-claim?token={swap.adapter_token}',
                'fields': ['txid', 'revealed_secret'],
            },
        },
        'bob_hns_claim': {
            'adapter': 'bob-wallet',
            'version': 1,
            'network': 'hns',
            'chain': 'handshake',
            'swap_id': swap.id,
            'role': 'bob',
            'action': 'claim_hns_htlc_with_revealed_secret',
            'amount_hns': amounts['hns'],
            'amount_dollary': amounts['hns_dollary'],
            'secret_hash_sha256': swap.secret_hash,
            'revealed_secret': swap.revealed_secret,
            'alice_lock_txid': swap.alice_lock_txid,
            'alice_lock_output_index': swap.hns_lock_output_index,
            'claim_public_key': swap.hns_claim_public_key,
            'refund_public_key': swap.hns_refund_public_key,
            'refund_locktime': swap.hns_refund_locktime,
            'htlc_address': swap.hns_lock_address,
            'htlc_script': swap.hns_lock_script,
            'htlc_value': swap.hns_lock_value,
            'metadata_callback': {
                'method': 'POST',
                'url': f'{app_base_url}/api/swaps/{swap.id}/hns-htlc?token={swap.adapter_token}',
                'fields': ['hns_claim_public_key'],
            },
            'callback': {
                'method': 'POST',
                'url': f'{app_base_url}/api/swaps/{swap.id}/txids/bob-claim?token={swap.adapter_token}',
                'field': 'txid',
            },
        },
    }


def build_swap_intents(swap, app_base_url):
    return {
        'swap_id': swap.id,
        'status': swap.status,
        'secret_hash_sha256': swap.secret_hash,
        'amounts': _swap_amounts(swap),
        'participants': _participants(swap),
        'hns_htlc': {
            'claim_public_key': swap.hns_claim_public_key,
            'refund_public_key': swap.hns_refund_public_key,
            'refund_locktime': swap.hns_refund_locktime,
            'lock_address': swap.hns_lock_address,
            'lock_script': swap.hns_lock_script,
            'lock_value': swap.hns_lock_value,
            'lock_output_index': swap.hns_lock_output_index,
        },
        'hns_lock': build_bob_hns_intent(swap, app_base_url) if swap.secret_hash else None,
        'btc_lock': build_bitcoin_psbt_intent(swap, app_base_url) if swap.secret_hash else None,
        'claims': build_claim_intents(swap, app_base_url) if swap.secret_hash else None,
    }
