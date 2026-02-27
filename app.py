from flask import Flask
from config import config
from models import db

def create_app(config_name='default'):
    app = Flask(__name__)
    
    # Ensure config_name is valid
    if config_name not in config:
        print(f"Warning: Config '{config_name}' not found. Using 'default'.", flush=True)
        config_name = 'default'
        
    app.config.from_object(config[config_name])

    db.init_app(app)

    from routes.auth import auth_bp
    app.register_blueprint(auth_bp)

    from routes.main import main_bp
    app.register_blueprint(main_bp)

    from routes.admin import admin_bp
    app.register_blueprint(admin_bp)

    return app
