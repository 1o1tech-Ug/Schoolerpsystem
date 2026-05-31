"""
app/models/reportcards.py
=========================
Report card models — updated to support:
  - exam_type  : explicit column (BOT / MID / EOT)
  - local_path : path to locally stored HTML/PDF file under /static/report_cards/
"""

from app.extensions import db
from datetime import datetime
from sqlalchemy import UniqueConstraint


class SchoolDetail(db.Model):
    __tablename__ = "school_details"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(
        db.Integer, db.ForeignKey("schools.id"), nullable=False, unique=True
    )

    # Core Identity & Contact Information
    school_logo_url = db.Column(db.String(255))
    po_box_number   = db.Column(db.String(50))
    district        = db.Column(db.String(100))
    contact_1       = db.Column(db.String(20), nullable=False)
    contact_2       = db.Column(db.String(20))
    website_domain  = db.Column(db.String(255))
    email           = db.Column(db.String(100))

    # Academic Threshold Requirements (secondary schools)
    # Academic Threshold Requirements (secondary schools)
    gp_min_mark       = db.Column(db.Float, default=50.0)
    ict_min_mark      = db.Column(db.Float, default=50.0)
    sub_math_min_mark = db.Column(db.Float, default=50.0)   # ← ADD THIS

    # Metadata
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    school = db.relationship(
        "School", backref=db.backref("details", uselist=False)
    )


class ReportCard(db.Model):
    __tablename__ = "report_cards"

    id = db.Column(db.Integer, primary_key=True)

    school_id  = db.Column(db.Integer, nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    term_id    = db.Column(db.Integer, nullable=False)

    # ── NEW: explicit exam_type column ──────────────────────────
    exam_type = db.Column(db.String(10))          # "BOT", "MID", "EOT"

    academic_year = db.Column(db.String(20))

    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    generated_by = db.Column(db.Integer)

    # ── Bunny / CDN URL (kept for backward-compat; optional) ────
    firebase_url  = db.Column(db.Text)
    firebase_path = db.Column(db.String(500))

    # ── NEW: local file path under static/report_cards/ ────────
    local_path = db.Column(db.String(500))

    status = db.Column(db.String(20), default="generated")

    # ── Unique constraint: one report per student/term/exam ─────
    __table_args__ = (
        UniqueConstraint(
            "school_id", "student_id", "term_id", "exam_type",
            name="uq_report_card_student_term_exam",
        ),
    )


class PrimaryReportSummary(db.Model):
    """
    Pre-computed aggregates for primary school students.
    Calculated once per generation and cached here.
    """
    __tablename__ = "report_summaries"

    id         = db.Column(db.Integer, primary_key=True)
    school_id  = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    term_id    = db.Column(db.Integer, db.ForeignKey("terms.id"),    nullable=False)
    year_id    = db.Column(db.Integer, db.ForeignKey("academic_years.id"), nullable=False)

    # Performance Metrics
    average_mark = db.Column(db.Float)
    position     = db.Column(db.Integer)
    aggregates   = db.Column(db.Integer)
    division     = db.Column(db.String(20))
    exam_type    = db.Column(db.String(20), nullable=False)

    # Relationships
    school       = db.relationship("School",       backref="report_summaries")
    student      = db.relationship("Student",      backref="report_summaries")
    term         = db.relationship("Term",         backref="report_summaries")
    academic_year = db.relationship("AcademicYear", backref="report_summaries")

    __table_args__ = (
        UniqueConstraint(
            "student_id", "term_id", "year_id", "exam_type",
            name="uq_student_term_exam_summary",
        ),
    )