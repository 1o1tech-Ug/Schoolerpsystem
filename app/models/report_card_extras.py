"""
app/models/report_card_extras.py
=================================
New tables supporting report-card editing features:

  1. SIGNATURES
     - HeadteacherSignature: one row per school.
     - ClassTeacherSignature: one row per stream.
     Both store only a Bunny CDN URL — same pattern as report card PDFs
     and the school logo elsewhere in this app.

  2. REPORT CARD OVERRIDES
     - ReportCardOverride: one row per (school, student, term, exam_type).
       Holds ONLY human-editable fields:
         * attendance_present / attendance_total
         * class_teacher_comment / headteacher_comment (free text)
         * class_teacher_initials / headteacher_initials (sign-off initials)
         * subject_initials — a JSON dict {subject_id: "XY"}, since the
           Primary report has a per-subject "INITIAL" column (the marking
           teacher's initials for that specific subject), which is a
           different thing from the class teacher's own sign-off initials.
       Deliberately does NOT hold marks/grades/positions — those stay
       computed-only.

  3. COMMENT BANK
     - ReportCommentBank: a small reusable list of canned comments per
       school, split by comment_type ("class_teacher" | "headteacher").
       Populates the "choose an existing comment" dropdown in the report
       editor instead of forcing staff to type from scratch every time.
"""

from datetime import datetime
from app.extensions import db


class HeadteacherSignature(db.Model):
    __tablename__ = "headteacher_signature"

    id            = db.Column(db.Integer, primary_key=True)
    school_id     = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False, unique=True)
    teacher_name  = db.Column(db.String(120))
    signature_url = db.Column(db.String(500))
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by    = db.Column(db.Integer)


class ClassTeacherSignature(db.Model):
    __tablename__ = "class_teacher_signature"

    id            = db.Column(db.Integer, primary_key=True)
    school_id     = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False)
    stream_id     = db.Column(db.Integer, db.ForeignKey("streams.id"), nullable=False, unique=True)
    teacher_name  = db.Column(db.String(120))
    signature_url = db.Column(db.String(500))
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by    = db.Column(db.Integer)

    __table_args__ = (
        db.Index("ix_class_teacher_signature_school", "school_id"),
    )


class ReportCardOverride(db.Model):
    __tablename__ = "report_card_override"

    id         = db.Column(db.Integer, primary_key=True)
    school_id  = db.Column(db.Integer, nullable=False)
    student_id = db.Column(db.Integer, nullable=False)
    term_id    = db.Column(db.Integer, nullable=False)
    exam_type  = db.Column(db.String(10), nullable=False)

    # Attendance overrides — null means "use the system-computed value"
    attendance_present = db.Column(db.Integer)
    attendance_total    = db.Column(db.Integer)

    # Free-text overrides
    class_teacher_comment  = db.Column(db.Text)
    headteacher_comment    = db.Column(db.Text)
    class_teacher_initials = db.Column(db.String(20))
    headteacher_initials   = db.Column(db.String(20))

    # [NEW] Per-subject marking-teacher initials, e.g. Primary report's
    # "INITIAL" column. Stored as {"<subject_id>": "XY", ...} rather than
    # a whole extra table, since it's a small, sparse, per-report set of
    # short strings with no independent lifecycle of its own.
    subject_initials = db.Column(db.JSON, default=dict)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer)

    __table_args__ = (
        db.UniqueConstraint(
            "school_id", "student_id", "term_id", "exam_type",
            name="uq_report_card_override_identity",
        ),
        db.Index("ix_report_card_override_lookup", "school_id", "student_id", "term_id", "exam_type"),
    )

    def to_dict(self) -> dict:
        return {
            "attendance_present":     self.attendance_present,
            "attendance_total":       self.attendance_total,
            "class_teacher_comment":  self.class_teacher_comment or "",
            "headteacher_comment":    self.headteacher_comment or "",
            "class_teacher_initials": self.class_teacher_initials or "",
            "headteacher_initials":   self.headteacher_initials or "",
            "subject_initials":       self.subject_initials or {},
        }


class ReportCommentBank(db.Model):
    """
    Reusable canned comments staff can pick from instead of typing a new
    comment every time. Scoped per school so each school builds its own
    list. `comment_type` separates class-teacher phrasing from
    headteacher phrasing since they're conventionally different in tone.
    """
    __tablename__ = "report_comment_bank"

    id           = db.Column(db.Integer, primary_key=True)
    school_id    = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False)
    comment_type = db.Column(db.String(20), nullable=False)  # "class_teacher" | "headteacher"
    text         = db.Column(db.Text, nullable=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    created_by   = db.Column(db.Integer)

    __table_args__ = (
        db.Index("ix_report_comment_bank_lookup", "school_id", "comment_type"),
    )

    def to_dict(self) -> dict:
        return {"id": self.id, "comment_type": self.comment_type, "text": self.text}