

import os

from datetime import timedelta
from dotenv import load_dotenv
load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_TOKEN_LOCATION = ["cookies"]
    JWT_COOKIE_SECURE = os.getenv("FLASK_ENV") == "production"  # True in prod, False in dev
    JWT_COOKIE_SAMESITE = "None" if os.getenv("FLASK_ENV") == "production" else "Lax"
    JWT_ACCESS_COOKIE_PATH = "/"
    JWT_REFRESH_COOKIE_PATH = "/"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)
    JWT_COOKIE_CSRF_PROTECT = False
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
    BUNNY_STORAGE_ZONE = os.getenv("BUNNY_STORAGE_ZONE")
    BUNNY_STORAGE_PASSWORD = os.getenv("BUNNY_STORAGE_PASSWORD")
    BUNNY_BASE_URL = os.getenv("BUNNY_BASE_URL")

    # CORS — Render domain pattern + local dev
    CORS_ORIGINS = [
        "https://1o1tech.onrender.com",   # your exact Render domain
        "http://localhost:5000",           # Flask dev server
        "http://127.0.0.1:5000",
    ]