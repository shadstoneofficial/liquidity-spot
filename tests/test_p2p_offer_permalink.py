import unittest
from datetime import datetime
from unittest.mock import patch

from app import create_app
from models import P2POffer, P2PTrade, User, db
from services.gems_service import GemsServiceError


class P2POfferPermalinkTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def _post_buy_offer(self, client):
        return client.post('/p2p/offers', base_url='https://liquidity.spot', data={
            'side': 'buy',
            'amount_hns': '4444',
            'price': '0.000000020319',
            'gems_stake': '0',
            'payment_method': 'Manual Wallet Transfer',
            'notes': 'Looking for HNS to send to Bob Wallet.',
        })

    def _sign_in_gfavip_user(self, client, user_id='maker', username='Maker'):
        with self.app.app_context():
            db.session.add(User(id=user_id, username=username, tier='paid', gems_balance=10))
            db.session.commit()
        with client.session_transaction() as user_session:
            user_session['user_id'] = user_id
            user_session['username'] = username
            user_session['tier'] = 'paid'
            user_session['auth_method'] = 'gfavip'
            user_session['token'] = 'test-token'

    def _post_bonded_offer(self, client, gems_stake='2', follow_redirects=False):
        return client.post('/p2p/offers', data={
            'side': 'buy',
            'amount_hns': '1000',
            'price': '0.00000004',
            'gems_stake': gems_stake,
            'payment_method': 'Manual Wallet Transfer',
            'notes': 'Bonded offer.',
        }, follow_redirects=follow_redirects)

    def test_new_offer_redirects_owner_to_shareable_permalink(self):
        owner = self.app.test_client()

        response = self._post_buy_offer(owner)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/p2p/offers/1')

        detail = owner.get(response.headers['Location'], base_url='https://liquidity.spot')
        body = detail.get_data(as_text=True)
        self.assertEqual(detail.status_code, 200)
        self.assertIn('Waiting for an HNS seller', body)
        self.assertIn('Your offer is live, but it has not been matched yet.', body)
        self.assertIn('https://liquidity.spot/p2p/offers/1', body)
        self.assertIn('Approximately 9,030 sats', body)

    def test_shared_permalink_tells_counterparty_how_to_accept(self):
        owner = self.app.test_client()
        self._post_buy_offer(owner)
        visitor = self.app.test_client()

        detail = visitor.get('/p2p/offers/1', base_url='https://liquidity.spot')
        body = detail.get_data(as_text=True)

        self.assertEqual(detail.status_code, 200)
        self.assertIn('Another person with HNS must accept this offer', body)
        self.assertIn('Sell HNS to this buyer', body)
        self.assertIn('Accepting creates a shared coordination room', body)

    def test_accepting_permalink_matches_offer_and_opens_trade_room(self):
        owner = self.app.test_client()
        self._post_buy_offer(owner)
        counterparty = self.app.test_client()

        response = counterparty.post('/p2p/offers/1/accept')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/p2p/trades/1')
        with self.app.app_context():
            offer = db.session.get(P2POffer, 1)
            trade = db.session.get(P2PTrade, 1)
            self.assertEqual(offer.status, 'matched')
            self.assertEqual(trade.status, 'matched')

        detail = counterparty.get('/p2p/offers/1')
        body = detail.get_data(as_text=True)
        self.assertIn('Offer matched', body)
        self.assertIn('Enter Trade Room', body)

    def test_gems_guide_explains_bonds_without_requiring_login(self):
        response = self.app.test_client().get('/gems')
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('What are Gems?', body)
        self.assertIn('Gems are not HNS or BTC', body)
        self.assertIn('The taker never pays the maker', body)

    def test_bonded_offer_locks_maker_gems_before_going_live(self):
        maker = self.app.test_client()
        self._sign_in_gfavip_user(maker)

        with patch('routes.main.is_wallet_service_configured', return_value=True), \
                patch('routes.main.wallet_deduct_gems', return_value={}) as deduct:
            response = self._post_bonded_offer(maker)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/p2p/offers/1')
        deduct.assert_called_once()
        self.assertEqual(deduct.call_args.args[:2], ('maker', 2))
        with self.app.app_context():
            offer = db.session.get(P2POffer, 1)
            self.assertEqual(offer.status, 'open')
            self.assertEqual(offer.maker_bond_status, 'locked')
            self.assertIsNotNone(offer.maker_bond_locked_at)

    def test_insufficient_gems_prevents_offer_from_going_live(self):
        maker = self.app.test_client()
        self._sign_in_gfavip_user(maker)

        with patch('routes.main.is_wallet_service_configured', return_value=True), \
                patch(
                    'routes.main.wallet_deduct_gems',
                    side_effect=GemsServiceError('User has 1 gems but 2 are required'),
                ):
            response = self._post_bonded_offer(maker, follow_redirects=True)

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Your Gems bond could not be locked', body)
        self.assertIn('Offer #1 was not posted', body)
        self.assertIn('You need 2 Gems available', body)
        with self.app.app_context():
            offer = db.session.get(P2POffer, 1)
            self.assertEqual(offer.status, 'bond_failed')
            self.assertEqual(offer.maker_bond_status, 'failed')
            self.assertEqual(P2POffer.query.filter_by(status='open').count(), 0)

    def test_accepting_funded_offer_does_not_charge_maker_again(self):
        with self.app.app_context():
            maker = User(id='maker', username='Maker', tier='paid', gems_balance=8)
            offer = P2POffer(
                creator_id='maker',
                side='buy',
                amount_hns='1000',
                price_btc_per_hns='0.00000004',
                gems_stake=2,
                payment_method='Manual Wallet Transfer',
                status='open',
                maker_bond_status='locked',
                maker_bond_locked_at=datetime.utcnow(),
            )
            db.session.add_all([maker, offer])
            db.session.commit()

        taker = self.app.test_client()
        with patch('routes.main.wallet_deduct_gems') as deduct:
            response = taker.post('/p2p/offers/1/accept')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/p2p/trades/1')
        deduct.assert_not_called()
        with self.app.app_context():
            trade = db.session.get(P2PTrade, 1)
            self.assertEqual(trade.maker_bond_status, 'locked')
            self.assertEqual(trade.maker_bond_amount, 2)

    def test_unfunded_legacy_bond_cannot_be_accepted(self):
        with self.app.app_context():
            maker = User(id='maker', username='Maker', tier='paid', gems_balance=10)
            offer = P2POffer(
                creator_id='maker',
                side='buy',
                amount_hns='1000',
                price_btc_per_hns='0.00000004',
                gems_stake=2,
                payment_method='Manual Wallet Transfer',
                status='open',
                maker_bond_status='not_locked',
            )
            db.session.add_all([maker, offer])
            db.session.commit()

        response = self.app.test_client().post('/p2p/offers/1/accept', follow_redirects=True)
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('This bonded offer is not available', body)
        self.assertIn('maker did not lock the listed Gems', body)
        with self.app.app_context():
            self.assertIsNone(db.session.get(P2PTrade, 1))

    def test_canceling_unmatched_offer_refunds_locked_bond(self):
        maker = self.app.test_client()
        self._sign_in_gfavip_user(maker)
        with self.app.app_context():
            offer = P2POffer(
                creator_id='maker',
                side='buy',
                amount_hns='1000',
                price_btc_per_hns='0.00000004',
                gems_stake=2,
                payment_method='Manual Wallet Transfer',
                status='open',
                maker_bond_status='locked',
                maker_bond_locked_at=datetime.utcnow(),
            )
            db.session.add(offer)
            db.session.commit()

        with patch('routes.main.is_wallet_service_configured', return_value=True), \
                patch('routes.main.wallet_credit_gems', return_value={}) as credit:
            response = maker.post('/p2p/offers/1/cancel')

        self.assertEqual(response.status_code, 302)
        credit.assert_called_once()
        self.assertEqual(credit.call_args.args[:2], ('maker', 2))
        with self.app.app_context():
            offer = db.session.get(P2POffer, 1)
            self.assertEqual(offer.status, 'canceled')
            self.assertEqual(offer.maker_bond_status, 'refunded')
            self.assertEqual(offer.maker_bond_resolution, 'offer_canceled')


if __name__ == '__main__':
    unittest.main()
