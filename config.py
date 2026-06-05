from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = "dev-secret-key"
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR / 'health_dynamics.db'}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
