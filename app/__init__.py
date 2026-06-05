from flask import Flask
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def create_app(config_object: str = "config.Config") -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)

    db.init_app(app)

    from app.views import main_bp

    app.register_blueprint(main_bp)

    with app.app_context():
        db.create_all()

    return app
