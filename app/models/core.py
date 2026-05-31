from app.extensions import db
from datetime import datetime


class School(db.Model):
    __tablename__ = "schools"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    school_code = db.Column(db.String(50), unique=True, nullable=False)
    school_type = db.Column(db.String(50), nullable=False)
    motto = db.Column(db.String(255), nullable=True)
    address = db.Column(db.Text, nullable=True)
    domain = db.Column(db.String(120), unique=True, nullable=True)
    status = db.Column(db.String(20), default="active")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # ── Auth / Users ──────────────────────────────────────────
    users = db.relationship("User", backref="school", lazy=True)
    student_auths = db.relationship("StudentAuth", backref="school", lazy=True)
    school_admin = db.relationship("Admin", backref="school", lazy=True)

    # ── Core ──────────────────────────────────────────────────
    subscription = db.relationship("Subscription", backref="school", uselist=False, lazy=True)
    blacklist = db.relationship("Blacklist", backref="school", uselist=False, lazy=True)  # FIX: removed duplicate relationship from Blacklist model
    notifications = db.relationship("Notification", backref="school", lazy=True)  # FIX: uselist=False -> True (one-to-many)

    # ── People ────────────────────────────────────────────────
    students = db.relationship("Student", backref="school", lazy=True)
    staff_members = db.relationship("Staff", backref="school", lazy=True)

    # ── Academic linking tables ───────────────────────────────
    teacher_subject_link = db.relationship("TeacherSubject", backref="school", lazy=True)   # FIX: uselist=False -> True
    teacher_stream_link = db.relationship("TeacherStream", backref="school", lazy=True)     # FIX: uselist=False -> True
    student_subject_link = db.relationship("StudentSubject", backref="school", lazy=True)   # FIX: uselist=False -> True
    student_stream_link = db.relationship("StudentStream", backref="school", lazy=True)     # FIX: uselist=False -> True

    # ── Academic content ─────────────────────────────────────
    papers = db.relationship("Papers", backref="school", lazy=True)      # FIX: uselist=False -> True
    subjects = db.relationship("Subject", backref="school", lazy=True)   # FIX: uselist=False -> True

    # ── Attendance ────────────────────────────────────────────
    staff_attendance = db.relationship("StaffAttendance", backref="school", lazy=True)        # FIX: uselist=False -> True
    lesson_sessions = db.relationship("LessonSession", backref="school", lazy=True)           # FIX: uselist=False -> True; now valid (school_id added to LessonSession)
    student_attendance = db.relationship("StudentAttendance", backref="school", lazy=True)    # FIX: uselist=False -> True; now valid (school_id added to StudentAttendance)

    # ── Assignments ───────────────────────────────────────────
    teacher_assignments = db.relationship("TeachAssignment", backref="school", lazy=True)  # FIX: uselist=False -> True; TeachingAssignment -> TeachAssignment

    # ── Assessments & Marks ───────────────────────────────────
    assessments = db.relationship("Assessment", backref="school", lazy=True)       # FIX: uselist=False -> True
    student_marks = db.relationship("StudentMark", backref="school", lazy=True)    # FIX: uselist=False -> True
    grading_scale = db.relationship("GradeScale", backref="school", lazy=True)    # FIX: uselist=False -> True


class Admin(db.Model):
    __tablename__ = "admins"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False)
    username = db.Column(db.String(80), nullable=False)
    contact = db.Column(db.String(30), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), default="active")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("school_id", "username", name="uq_admin_school_username"),
    )


class Subscription(db.Model):
    __tablename__ = "subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False)
    payment_plan = db.Column(db.String(50), nullable=False)
    payment_status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class UserModule(db.Model):
    __tablename__ = "user_modules"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    module_name = db.Column(db.String(100))


class Blacklist(db.Model):
    __tablename__ = "blacklist"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False)
    reason = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=db.func.now())
    # FIX: removed duplicate `school = db.relationship(...)` — backref="school"
    # on School.blacklist already creates the reverse accessor on this model


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # FIX: removed duplicate `school` relationship — backref on School.notifications handles it
