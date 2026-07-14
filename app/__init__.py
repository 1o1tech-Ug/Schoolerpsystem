"""
app/__init__.py
================
Application factory.

CHANGES vs original:
  1. setup_logging(app)               — initialise rotating-file logger
  2. register_error_handlers(app)     — centralised JSON error responses
  3. register_rate_limit_handlers(app)— JSON 429 + breach logging
  4. limiter.init_app(app)            — already present, kept as-is
"""

from flask import Flask
from app.extensions import db, migrate, login_manager, jwt, limiter
from flask_cors import CORS

def create_app():
    app = Flask(__name__)
    app.config.from_object("app.config.Config")

    # ── CORS ─────────────────────────────────────────────────────────────────
    CORS(
        app,
        origins=app.config["CORS_ORIGINS"],
        supports_credentials=True,   # required for cookie-based JWT
        allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )

    # ── Core extensions ──────────────────────────────────────────────────────
    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    limiter.init_app(app)

    # ── Logging — must come early so every subsequent log call is captured ───
    from app.core.logging_config import setup_logging
    setup_logging(app)

    # ── Centralised error handling ────────────────────────────────────────────
    from app.core.error_handlers import register_error_handlers
    register_error_handlers(app)

    # ── Rate-limit 429 → JSON ────────────────────────────────────────────────
    from app.core.rate_limit import register_rate_limit_handlers
    register_rate_limit_handlers(app)

    # ── JWT handler & blocklist ──────────────────────────────────────────────
    from app import jwt_handler  # noqa: F401  (registers @jwt callbacks)

    # ── Model imports (needed for Flask-Migrate to see all tables) ───────────
    from app.models.user import User, StudentAuth
    from app.models.core import School, Admin, Subscription, UserModule, Notification, Blacklist
    from app.models.people import (
        Student, StudentAcademic, Guardian,
        MedicalRecord, Document, Staff,
    )
    from app.models.report_card_extras import (
    HeadteacherSignature,ClassTeacherSignature,ReportCardOverride,ReportCommentBank,
    )
    from app.models.academic_structure import (
        Class, Stream, AcademicConfig, AcademicYear, Term,
        Subject, TeacherSubject, Papers, TeacherStream,
        StudentSubject, StudentStream,StudentDailyAttendance, StaffAttendance,
        TeachAssignment,
        LessonSession, StudentAttendance,
        AssessmentType, Assessment, StudentMark, GradeScale, StudentEnrollment,
    )
    from app.models.blocklist import TokenBlocklist
    from app.models.reportcards import SchoolDetail, ReportCard, PrimaryReportSummary
    from app.models.finance import Invoice, Payment, Receipt, Expenses, FeeStructure

    # ── Middleware ────────────────────────────────────────────────────────────
    from app.middleware.before_request import register_jwt_refresh
    register_jwt_refresh(app)

    # ── Route blueprints ─────────────────────────────────────────────────────
    from .routes.studentManagement import student_Management
    app.register_blueprint(student_Management)

    from .routes.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix="/auth")

    from app.routes.views import views_bp
    app.register_blueprint(views_bp)

    from app.routes.admin import admin_bp
    app.register_blueprint(admin_bp)

    # ── API blueprints ────────────────────────────────────────────────────────
    from .apis.student_management import student_management_api
    from .apis.super_admin_apis import school_api
    app.register_blueprint(school_api)
    app.register_blueprint(student_management_api)

    from .apis.settings import settings, settings_api
    app.register_blueprint(settings)
    app.register_blueprint(settings_api)

    from .apis.academics_api import academics_api
    app.register_blueprint(academics_api)

    from .apis.academics_api_2 import academics_api_2
    app.register_blueprint(academics_api_2)

    from .apis.finance_api import finance_bp
    app.register_blueprint(finance_bp)

    from .apis.teachers_apis import teachers_api
    app.register_blueprint(teachers_api)

    from .apis.reportcardgeneration import report_cards_api
    app.register_blueprint(report_cards_api, url_prefix="/api")

    from .apis.student_promotion import promotion_api
    app.register_blueprint(promotion_api)

    from .apis.alumni import alumni_api
    app.register_blueprint(alumni_api)

    from .apis.sendreportcards import send_reports_api
    app.register_blueprint(send_reports_api, url_prefix="/api")

    from .apis.studentsportal import student_portal
    app.register_blueprint(student_portal)

    return app