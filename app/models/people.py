from app.extensions import db
from sqlalchemy import UniqueConstraint
from datetime import datetime
from app.utils.bunny import public_file_url


class Student(db.Model):
    __tablename__ = 'students'

    id = db.Column(db.Integer, primary_key=True)

    student_code = db.Column(db.String(10), nullable=False)
    admission_number = db.Column(db.String(50), nullable=False)

    first_name = db.Column(db.String(30), nullable=False)
    last_name = db.Column(db.String(30), nullable=False)
    gender = db.Column(db.String(15))
    date_of_birth = db.Column(db.Date)
    nationality = db.Column(db.String(20))
    nin = db.Column(db.String(50))
    residence = db.Column(db.String(50))
    enrollment_type = db.Column(db.String(20))
    student_type = db.Column(db.String(20), default="day")

    # [FIX][STORAGE] Underlying DB column is still literally named
    # "photo_url" (see db.Column("photo_url", ...) below) — only the
    # Python-side attribute is renamed, so no migration is needed.
    # New uploads store a relative Bunny remote_path; older rows still
    # hold a full CDN URL from before that change. The photo_url
    # property below resolves either shape to a renderable URL on read
    # (public_file_url() passes full URLs through unchanged), so every
    # existing template that does {{ student.photo_url }} keeps working
    # unchanged, and every existing
    # `student.photo_url = _upload_image(...)` assignment keeps working
    # unchanged too (it goes through the setter).
    _photo_url = db.Column("photo_url", db.String(255), nullable=True)

    school_id = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'), nullable=True)

    # Relationships
    invoices = db.relationship(
    'Invoice',
    backref='student',
    cascade='all, delete-orphan',
    passive_deletes=True
)
    academic = db.relationship("StudentAcademic", backref="student", uselist=False)
    guardian = db.relationship("Guardian", backref="student", lazy=True)
    medical_record = db.relationship("MedicalRecord", backref="student", uselist=False)
    documents = db.relationship("Document", backref="student", lazy=True)
    stream = db.relationship('StudentStream', backref='student', lazy=True)
    subjects = db.relationship('StudentSubject', backref='student', lazy=True)

    __table_args__ = (
        UniqueConstraint('school_id', 'admission_number', name='uq_school_admission_number'),
        UniqueConstraint('school_id', 'student_code', name='uq_student_code_per_school'),
    )

    @property
    def photo_url(self):
        return public_file_url(self._photo_url)

    @photo_url.setter
    def photo_url(self, value):
        self._photo_url = value


class StudentAcademic(db.Model):
    __tablename__ = 'student_academic'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete="CASCADE"), nullable=False)

    class_id = db.Column(db.Integer, db.ForeignKey('classes.id'))
    stream_id = db.Column(db.Integer, db.ForeignKey('streams.id'))

    house = db.Column(db.String(20))
    level = db.Column(db.String(50))
    enrollment_status = db.Column(db.String(50))
    status = db.Column(db.String(30))
    date_of_admission = db.Column(db.Date)
    academic_year = db.Column(db.String(20))


class Guardian(db.Model):
    __tablename__ = 'guardians'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)

    name = db.Column(db.String(50), nullable=False)
    relationship = db.Column(db.String(50))
    contact = db.Column(db.String(20))
    address = db.Column(db.Text)
    occupation = db.Column(db.String(50))

    # [FIX][STORAGE] Same resolve-on-read pattern as Student.photo_url —
    # see the comment there for why.
    _photo_url = db.Column("photo_url", db.String(255))

    @property
    def photo_url(self):
        return public_file_url(self._photo_url)

    @photo_url.setter
    def photo_url(self, value):
        self._photo_url = value


class MedicalRecord(db.Model):
    __tablename__ = 'medical_records'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)

    has_asthma = db.Column(db.Boolean, default=False)
    has_heart_problem = db.Column(db.Boolean, default=False)
    has_sickle_cell = db.Column(db.Boolean, default=False)
    has_hiv = db.Column(db.Boolean, default=False)
    other_conditions = db.Column(db.String(160))


class Document(db.Model):
    __tablename__ = 'documents'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)

    document_type = db.Column(db.String(50))

    # [FIX][STORAGE] Same resolve-on-read pattern as Student.photo_url.
    # nullable=False is preserved on the underlying column exactly as
    # before — the property/setter pair doesn't change that constraint.
    _file_url = db.Column("file_url", db.Text, nullable=False)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def file_url(self):
        return public_file_url(self._file_url)

    @file_url.setter
    def file_url(self, value):
        self._file_url = value


class Staff(db.Model):
    __tablename__ = 'staff'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    staff_code = db.Column(db.String(20), nullable=False)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    gender = db.Column(db.String(10))
    phone = db.Column(db.String(20))
    photo_url = db.Column(db.String(255))
    staff_type = db.Column(db.String(20), nullable=False)  # "teaching" / "non-teaching"

    # Relationships
    subjects_link = db.relationship('TeacherSubject', backref='staff', lazy=True)
    stream_link = db.relationship('TeacherStream', backref='staff', lazy=True)
    staff_attendance = db.relationship('StaffAttendance', backref='staff', lazy=True)  # FIX: was 'StaffAttendance' with wrong backref collision
    teachers_assignment = db.relationship('TeachAssignment', backref='staff', lazy=True)  # FIX: TeachingAssignment -> TeachAssignment

    __table_args__ = (
        UniqueConstraint('staff_code', 'school_id', name='uq_staff_code_school_id'),
    )