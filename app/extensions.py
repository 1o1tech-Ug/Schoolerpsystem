"""
app/extensions.py
==================
Central extension instances.  Import from here everywhere in the app
so there is exactly one instance of each extension.

CHANGED: Added `limiter` import from app.core.rate_limit.
         The limiter is initialised in create_app() via limiter.init_app(app).
"""

from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_jwt_extended import JWTManager

from app.core.rate_limit import limiter   # ← NEW

db           = SQLAlchemy()
migrate      = Migrate()
login_manager = LoginManager()
jwt          = JWTManager()

# limiter is already instantiated in app.core.rate_limit and re-exported here
# so existing `from app.extensions import limiter` imports continue to work.
__all__ = ["db", "migrate", "login_manager", "jwt", "limiter"]