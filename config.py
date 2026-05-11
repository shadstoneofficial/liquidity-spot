import os
from dotenv import load_dotenv

load_dotenv()

database_uri = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
if database_uri.startswith('postgres://'):
    database_uri = database_uri.replace('postgres://', 'postgresql://', 1)

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = database_uri
    GFAVIP_SERVICE_NAME = os.environ.get('GFAVIP_SERVICE_NAME', 'liquidity-spot')
    REDIRECT_URI = os.environ.get('REDIRECT_URI', 'http://localhost:8000/callback')
    GFAVIP_WALLET_API_KEY = os.environ.get('GFAVIP_WALLET_API_KEY')
    GFAVIP_WALLET_BASE_URL = os.environ.get('GFAVIP_WALLET_BASE_URL', 'https://wallet.gfavip.com')
    BTC_WATCHER_BASE_URL = os.environ.get('BTC_WATCHER_BASE_URL', 'https://blockstream.info/api')
    HNS_WATCHER_BASE_URL = os.environ.get('HNS_WATCHER_BASE_URL')
    ATOMIC_SWAP_NETWORK = os.environ.get('ATOMIC_SWAP_NETWORK', 'main')

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}
