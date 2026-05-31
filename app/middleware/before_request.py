from flask import request, after_this_request
from flask_jwt_extended import (
    verify_jwt_in_request, get_jwt, get_jwt_identity, 
    create_access_token, set_access_cookies
)
from jwt.exceptions import ExpiredSignatureError
from app.models.blocklist import TokenBlocklist 
from app.extensions import db

def register_jwt_refresh(app):
    @app.before_request
    def silent_refresh_logic():
        if any(x in request.path for x in ['/login', '/register', '/static', '/auth/refresh']):
            return

        cookie_name = app.config.get('JWT_ACCESS_COOKIE_NAME', 'access_token_cookie')
        if cookie_name not in request.cookies:
            return

        try:
            verify_jwt_in_request(optional=True)
        except ExpiredSignatureError:
            try:
                # 1. Verify Refresh Token exists and is technically valid (not expired)
                verify_jwt_in_request(refresh=True, locations=['cookies'])
                
                # 2. 🔥 NEW: Check if this specific Refresh Token JTI is revoked
                refresh_jwt = get_jwt()
                refresh_jti = refresh_jwt["jti"]
                
                is_revoked = db.session.query(TokenBlocklist.id).filter_by(jti=refresh_jti).scalar()
                if is_revoked:
                    print(f"❌ Blocked: Attempt to use revoked refresh token {refresh_jti}")
                    return # Stop here; don't heal the request

                # 3. Heal the request if not revoked
                identity = get_jwt_identity()
                new_access_token = create_access_token(identity=identity)

                request.cookies = dict(request.cookies) 
                request.cookies[cookie_name] = new_access_token

                def set_new_cookie(response):
                    set_access_cookies(response, new_access_token)
                    return response
                
                after_this_request(set_new_cookie)
                print(f"✅ Auto-healed token for: {identity}")
                
            except Exception as e:
                print(f"❌ Full session expired or missing: {e}")
