import unittest
from unittest.mock import patch

from app import create_app
from models import User, db
from services.http_client import ExternalHTTPDisabled, get as http_get


class TestEnvironmentTests(unittest.TestCase):
    def test_testing_config_uses_memory_database_and_no_external_credentials(self):
        app = create_app('testing')

        self.assertTrue(app.config['TESTING'])
        self.assertEqual(app.config['SQLALCHEMY_DATABASE_URI'], 'sqlite://')
        self.assertIsNone(app.config['GFAVIP_WALLET_API_KEY'])
        self.assertIsNone(app.config['BTC_WATCHER_BASE_URL'])
        self.assertIsNone(app.config['HNS_WATCHER_BASE_URL'])
        self.assertEqual(app.config['ATOMIC_SWAP_NETWORK'], 'regtest')
        self.assertFalse(app.config['ALLOW_EXTERNAL_HTTP'])

    def test_testing_databases_do_not_share_state_between_app_instances(self):
        first_app = create_app('testing')
        with first_app.app_context():
            db.create_all()
            db.session.add(User(id='temporary-user', username='Temporary User'))
            db.session.commit()
            self.assertIsNotNone(db.session.get(User, 'temporary-user'))

        second_app = create_app('testing')
        with second_app.app_context():
            db.create_all()
            self.assertIsNone(db.session.get(User, 'temporary-user'))
            db.session.remove()
            db.drop_all()

        with first_app.app_context():
            self.assertIsNotNone(db.session.get(User, 'temporary-user'))
            db.session.remove()
            db.drop_all()

    def test_testing_config_blocks_http_before_requests_transport(self):
        app = create_app('testing')

        with app.app_context(), patch('requests.sessions.Session.request') as transport:
            with self.assertRaisesRegex(ExternalHTTPDisabled, 'disabled'):
                http_get('https://example.invalid/should-not-be-called')

        transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
