import unittest

from app import create_app
from models import P2POffer, P2PTrade, db


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


if __name__ == '__main__':
    unittest.main()
