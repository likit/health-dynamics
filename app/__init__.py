import click
from flask import Flask
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def create_app(config_object: str = "config.Config") -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)

    db.init_app(app)
    from app import models
    from app.views import main_bp

    app.register_blueprint(main_bp)
    register_commands(app)

    return app


def register_commands(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_command() -> None:
        db.create_all()
        click.echo("Initialized the database.")
