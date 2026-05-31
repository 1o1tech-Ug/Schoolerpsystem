from app.extensions import db
from datetime import datetime
import enum
from sqlalchemy import UniqueConstraint, CheckConstraint

# =========================
# CLASS
# =========================
class Class(db.Model):
    __tablename__ = 'classes'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)

    streams = db.relationship('Stream', backref='class_', lazy=True)
    students = db.relationship('Student', backref='class_', lazy=True)


# =========================
# STREAM
# =========================
class Stream(db.Model):
    __tablename__ = 'streams'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(20), nullable=False)
    capacity = db.Column(db.Integer)

    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=False)

    students = db.relationship('StudentStream', backref='stream', lazy=True)
    teachers = db.relationship('TeacherStream', backref='stream', lazy=True)
    status = db.Column(db.String(20)) #deleted


# =========================
# ACADEMIC YEAR
# =========================
class AcademicYear(db.Model):
    __tablename__ = 'academic_years'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(10), nullable=False)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    is_active = db.Column(db.Boolean, default=False)


# =========================
# TERM
# =========================
class Term(db.Model):
    __tablename__ = 'terms'

    id = db.Column(db.Integer, primary_key=True)

    academic_year_id = db.Column(
        db.Integer,
        db.ForeignKey('academic_years.id'),
        nullable=False
    )

    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)

    name = db.Column(db.String(50))
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    status = db.Column(db.String(20))  # draft, active, locked


# =========================
# ACADEMIC CONFIG
# =========================
class AcademicConfig(db.Model):
    __tablename__ = "academic_configs"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)

    current_academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'))
    current_term_id = db.Column(db.Integer, db.ForeignKey('terms.id'))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# =========================
# SUBJECT
# =========================
class Subject(db.Model):
    __tablename__ = "subjects"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)

    name = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(100))
    level = db.Column(db.String(20))


# =========================
# PAPERS
# =========================
class Papers(db.Model):
    __tablename__ = "papers"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)

    paper_name = db.Column(db.String(50))
    max_marks = db.Column(db.Integer)


# =========================
# TEACHER SUBJECT
# =========================
class TeacherSubject(db.Model):
    __tablename__ = "teacher_subjects"

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)

    __table_args__ = (
        UniqueConstraint('teacher_id', 'subject_id', 'school_id',
                         name='uq_teacher_subject'),
    )


# =========================
# TEACHER STREAM
# =========================
class TeacherStream(db.Model):
    __tablename__ = 'teacher_streams'

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    stream_id = db.Column(db.Integer, db.ForeignKey('streams.id'), nullable=False)

    __table_args__ = (
        UniqueConstraint('teacher_id', 'stream_id', 'school_id',
                         name='uq_teacher_stream'),
    )


# =========================
# STUDENT SUBJECT
# =========================
class StudentSubject(db.Model):
    __tablename__ = "student_subjects"

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)

    __table_args__ = (
        UniqueConstraint('student_id', 'subject_id', 'school_id',
                         name='uq_student_subject'),
    )


# =========================
# STUDENT STREAM
# =========================
class StudentStream(db.Model):
    __tablename__ = "student_streams"

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    stream_id = db.Column(db.Integer, db.ForeignKey('streams.id'), nullable=False)

    __table_args__ = (
        UniqueConstraint('student_id', 'stream_id', 'school_id',
                         name='uq_student_stream'),
    )


# =========================
# STAFF ATTENDANCE
# =========================
class StaffAttendance(db.Model):
    __tablename__ = 'staff_attendance'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    academic_year_id = db.Column(db.Integer, db.ForeignKey('academic_years.id'), nullable=False)
    term_id = db.Column(db.Integer, db.ForeignKey('terms.id'), nullable=False)

    status = db.Column(db.String(20), nullable=False)  # "present", "late", "absent"
    time_in = db.Column(db.Time, nullable=True)
    notes = db.Column(db.Text)
    date = db.Column(db.Date, nullable=False)

    # Relationships
    # NOTE: 'school' backref is defined on School model via staff_attendance relationship
    # NOTE: 'staff' backref is defined on Staff model via staff_attendance relationship
    academic_year = db.relationship('AcademicYear', backref='staff_attendances')
    term = db.relationship('Term', backref='staff_attendances')

    __table_args__ = (
        UniqueConstraint('staff_id', 'date', name='uq_staff_daily_attendance'),
    )


# =========================
# TEACH ASSIGNMENT
# =========================
class TeachAssignment(db.Model):
    __tablename__ = 'teaching_assignments'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey('staff.id'), nullable=False)
    stream_id = db.Column(db.Integer, db.ForeignKey('streams.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)

    # Relationships
    # NOTE: 'school' backref is defined on School model
    # NOTE: 'staff' backref is defined on Staff model via teachers_assignment
    stream = db.relationship('Stream', backref='teacher_assignments')
    subject = db.relationship('Subject', backref='teacher_assignments')

    # One-to-Many: an assignment has many lesson sessions
    lessons = db.relationship('LessonSession', backref='assignment', lazy=True)


# =========================
# LESSON SESSION
# =========================
class LessonSession(db.Model):
    __tablename__ = 'lesson_sessions'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)  # FIX: added school_id
    assignment_id = db.Column(db.Integer, db.ForeignKey('teaching_assignments.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    period = db.Column(db.Integer)  # e.g., period 1, 2, 3

    # NOTE: 'assignment' backref is handled by TeachAssignment.lessons
    student_attendances = db.relationship('StudentAttendance', backref='lesson_session', lazy=True)


# =========================
# STUDENT ATTENDANCE
# =========================
class StudentAttendance(db.Model):
    __tablename__ = 'student_attendance'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)  # FIX: added school_id
    lesson_id = db.Column(db.Integer, db.ForeignKey('lesson_sessions.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    status = db.Column(db.String(20), nullable=False)  # "present", "absent"

    # NOTE: 'lesson_session' backref is handled by LessonSession.student_attendances
    student = db.relationship('Student', backref='lesson_attendances')

    __table_args__ = (
        UniqueConstraint('lesson_id', 'student_id', name='unique_attendance_per_lesson'),
    )


# =========================
# ASSESSMENT TYPE
# =========================
class AssessmentType(enum.Enum):
    BOT = "BOT"
    MID = "MID"
    EOT = "EOT"


# =========================
# ASSESSMENT
# =========================
class Assessment(db.Model):
    __tablename__ = 'assessments'

    id            = db.Column(db.Integer, primary_key=True)
    school_id     = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    assignment_id = db.Column(db.Integer, db.ForeignKey('teaching_assignments.id'), nullable=True)

    # Denormalised — populated always so the unique constraint works when
    # assignment_id is NULL (subject not yet assigned to a teacher).
    stream_id     = db.Column(db.Integer, db.ForeignKey('streams.id'), nullable=False)
    subject_id    = db.Column(db.Integer, db.ForeignKey('subjects.id'), nullable=False)

    term_id       = db.Column(db.Integer, db.ForeignKey('terms.id'), nullable=False)
    paper_id      = db.Column(db.Integer, db.ForeignKey('papers.id'), nullable=True)
    type          = db.Column(db.Enum(AssessmentType), nullable=False)
    max_score     = db.Column(db.Float, nullable=False)

    # Relationships
    assignment = db.relationship('TeachAssignment', backref='assessments')
    term       = db.relationship('Term', backref='assessments')
    paper      = db.relationship('Papers', backref='assessments')
    marks      = db.relationship('StudentMark', back_populates='assessment',
                                 cascade='all, delete-orphan')

    __table_args__ = (
        # Unique across subject+stream+term+type+paper regardless of whether
        # a teaching assignment exists yet.
        db.UniqueConstraint(
            "subject_id", "stream_id", "term_id", "type", "paper_id",
            name="uq_assessment"
        ),
    )
# =========================
# STUDENT MARK
# =========================
class StudentMark(db.Model):
    __tablename__ = 'student_marks'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    assessment_id = db.Column(db.Integer, db.ForeignKey('assessments.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    score = db.Column(db.Float, nullable=False)

    # Relationships
    # NOTE: 'school' backref is defined on School model
    assessment = db.relationship('Assessment', back_populates='marks')
    student = db.relationship('Student', backref='marks')

    __table_args__ = (
        UniqueConstraint('assessment_id', 'student_id', name='uq_student_mark_assessment'),
    )


# =========================
# GRADE SCALE
# =========================
class GradeScale(db.Model):
    __tablename__ = 'grade_scales'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)

    min_score = db.Column(db.Float, nullable=False)
    max_score = db.Column(db.Float, nullable=False)
    grade = db.Column(db.String(5), nullable=False)
    remark = db.Column(db.String(100))
    section_category = db.Column(db.String(30))#A level,O level,upper primary,lower primary,nursery



class StudentEnrollment(db.Model):
    __tablename__ = "student_enrollments"

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(
        db.Integer,
        db.ForeignKey("schools.id"),
        nullable=False
    )

    student_id = db.Column(
        db.Integer,
        db.ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False
    )

    academic_year_id = db.Column(
        db.Integer,
        db.ForeignKey("academic_years.id"),
        nullable=False
    )

    class_id = db.Column(
        db.Integer,
        db.ForeignKey("classes.id"),
        nullable=False
    )

    stream_id = db.Column(
        db.Integer,
        db.ForeignKey("streams.id"),
        nullable=True
    )

    status = db.Column(
        db.String(20),
        default="active"
    )
    # active
    # promoted
    # repeated
    # demoted
    # transferred
    # graduated

    previous_enrollment_id = db.Column(
        db.Integer,
        db.ForeignKey("student_enrollments.id"),
        nullable=True
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )