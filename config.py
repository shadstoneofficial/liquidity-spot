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

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}
