import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR / 'health_dynamics.db'}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
    LOCAL_LLM_MODEL = os.getenv(
        "LOCAL_LLM_MODEL",
        "ministral-3:3b",
    )
    LOCAL_LLM_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "")
