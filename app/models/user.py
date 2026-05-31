from app.extensions import db

class User(db.Model):

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    username = db.Column(db.String(100), nullable=False)

    password_hash = db.Column(db.String(255), nullable=False)

    role = db.Column(db.String(50), nullable=False)

    school_id = db.Column(
        db.Integer,
        db.ForeignKey("schools.id"),
        nullable=False
    )
    
    staff_id = db.Column(
        db.Integer,
        db.ForeignKey("staff.id"),
        nullable=True,
        unique=True
    )

    status = db.Column(
        db.String(20),
        default="active"
    )

    is_super_admin = db.Column(
        db.Boolean,
        default=False
    )

    user_modules = db.relationship(
        "UserModule",
        backref="users",
        lazy=True
    )
    
    staff = db.relationship(
        "Staff",
        backref=db.backref(
            "user_account",
            uselist=False
        )
    )

    __table_args__ = (
        db.UniqueConstraint(
            "username",
            "school_id",
            name="unique_username_per_school"
        ),
    )
    


class StudentAuth(db.Model):

    __tablename__ = "student_auth"

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(db.Integer, db.ForeignKey("schools.id"), nullable=False)

    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)

    password_hash = db.Column(db.String(255), nullable=False)

    term_id = db.Column(db.Integer, nullable=True)

    status = db.Column(db.String(20), default="active")
