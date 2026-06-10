from app.extensions import db
from sqlalchemy import UniqueConstraint
from datetime import datetime


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
    photo_url = db.Column(db.String(255), nullable=True)

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
    photo_url = db.Column(db.String(255))


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
    file_url = db.Column(db.Text, nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


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
