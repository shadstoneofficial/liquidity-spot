import unittest
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
        self.assertIn('The taker does not pay the maker', body)

    def test_failed_maker_bond_gives_taker_helpful_next_steps(self):
        with self.app.app_context():
            maker = User(id='maker', username='Maker', tier='paid', gems_balance=1)
            offer = P2POffer(
                creator_id='maker',
                side='buy',
                amount_hns='1000',
                price_btc_per_hns='0.00000004',
                gems_stake=2,
                payment_method='Manual Wallet Transfer',
                status='open',
            )
            db.session.add_all([maker, offer])
            db.session.commit()

        taker = self.app.test_client()
        with patch('routes.main.is_wallet_service_configured', return_value=True), \
                patch(
                    'routes.main.wallet_deduct_gems',
                    side_effect=GemsServiceError('User has 1 gems but 2 are required'),
                ):
            response = taker.post('/p2p/offers/1/accept', follow_redirects=True)

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('The offer creator does not have enough Gems', body)
        self.assertIn('No Gems, HNS, or BTC were taken from you', body)
        self.assertIn('Ask the offer creator to add Gems in GFAVIP', body)
        self.assertIn('What are Gems?', body)
        with self.app.app_context():
            self.assertEqual(db.session.get(P2POffer, 1).status, 'open')
            self.assertIsNone(db.session.get(P2PTrade, 1))


if __name__ == '__main__':
    unittest.main()
