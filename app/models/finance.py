from datetime import datetime
from app.extensions import db


class Invoice(db.Model):
    __tablename__ = 'invoices'

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(
        db.Integer,
        db.ForeignKey('schools.id'),
        nullable=False
    )

    student_id = db.Column(
    db.Integer,
    db.ForeignKey(
        'students.id',
        ondelete='CASCADE'
    ),
    nullable=False
)

    term_id = db.Column(
        db.Integer,
        db.ForeignKey('terms.id'),
        nullable=False
    )

    year_id = db.Column(
        db.Integer,
        db.ForeignKey('academic_years.id'),
        nullable=False
    )

    total_amount = db.Column(
        db.Float,
        nullable=False
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    # RELATIONSHIPS
    payments = db.relationship(
        'Payment',
        backref='invoice',
        lazy=True,
        cascade="all, delete-orphan"
    )

    items = db.relationship(
        "InvoiceItem",
        backref="invoice",
        lazy=True,
        cascade="all, delete-orphan"
    )

    @property
    def amount_paid(self):
        return sum(
            p.amount
            for p in self.payments
            if p.status == 'completed'
        )

    @property
    def balance(self):
        return self.total_amount - self.amount_paid


class Payment(db.Model):
    __tablename__ = 'payments'

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(
        db.Integer,
        db.ForeignKey('schools.id'),
        nullable=False
    )

    student_id = db.Column(
        db.Integer,
        db.ForeignKey('students.id'),
        nullable=False
    )

    invoice_id = db.Column(
        db.Integer,
        db.ForeignKey('invoices.id'),
        nullable=False
    )

    amount = db.Column(
        db.Float,
        nullable=False
    )

    method = db.Column(db.String(50))

    reference = db.Column(db.String(100))

    status = db.Column(
        db.String(20),
        default='completed'
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    receipt = db.relationship(
        'Receipt',
        backref='payment',
        uselist=False
    )


class Receipt(db.Model):
    __tablename__ = 'receipts'

    id = db.Column(db.Integer, primary_key=True)

    payment_id = db.Column(
        db.Integer,
        db.ForeignKey('payments.id'),
        nullable=False
    )

    receipt_number = db.Column(
        db.String(100),
        unique=True,
        nullable=False
    )

    issued_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )


class Expenses(db.Model):
    __tablename__ = 'expenses'

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(
        db.Integer,
        db.ForeignKey('schools.id'),
        nullable=False
    )

    term_id = db.Column(
        db.Integer,
        db.ForeignKey('terms.id')
    )

    year_id = db.Column(
        db.Integer,
        db.ForeignKey('academic_years.id')
    )

    title = db.Column(
        db.String(30),
        nullable=False
    )

    category = db.Column(
        db.String(30),
        nullable=False
    )

    amount = db.Column(
        db.Float,
        nullable=False
    )

    date = db.Column(
        db.Date,
        nullable=False
    )

    status = db.Column(
        db.String(20),
        default='Pending'
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    payment_method = db.Column(db.String(50))


class FeeStructure(db.Model):
    __tablename__ = 'fee_structures'

    id = db.Column(db.Integer, primary_key=True)

    school_id = db.Column(
        db.Integer,
        db.ForeignKey('schools.id'),
        nullable=False
    )

    class_id = db.Column(
        db.Integer,
        db.ForeignKey('classes.id'),
        nullable=False
    )

    term_id = db.Column(
        db.Integer,
        db.ForeignKey('terms.id'),
        nullable=False
    )

    academic_year_id = db.Column(
        db.Integer,
        db.ForeignKey('academic_years.id'),
        nullable=False
    )

    student_type = db.Column(
        db.String(20)
    )

    status = db.Column(
        db.String(20),
        default="draft"
    )

    total_amount = db.Column(
        db.Float,
        default=0
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    # RELATIONSHIPS
    class_ = db.relationship(
        "Class",
        backref="fee_structures"
    )

    term = db.relationship(
        "Term",
        backref="fee_structures"
    )

    academic_year = db.relationship(
        "AcademicYear",
        backref="fee_structures"
    )

    items = db.relationship(
        "FeeItem",
        backref="fee_structure",
        lazy=True,
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        db.UniqueConstraint(
            'school_id',
            'class_id',
            'term_id',
            'academic_year_id',
            'student_type',
            name='unique_fee_structure'
        ),
    )


class FeeItem(db.Model):
    __tablename__ = "fee_items"

    id = db.Column(db.Integer, primary_key=True)

    fee_structure_id = db.Column(
        db.Integer,
        db.ForeignKey('fee_structures.id'),
        nullable=False
    )

    fee_type = db.Column(
        db.String(50),
        nullable=False
    )

    amount = db.Column(
        db.Float,
        default=0
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    __table_args__ = (
        db.UniqueConstraint(
            'fee_structure_id',
            'fee_type',
            name='unique_fee_item'
        ),
    )


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"

    id = db.Column(db.Integer, primary_key=True)

    invoice_id = db.Column(
        db.Integer,
        db.ForeignKey('invoices.id'),
        nullable=False
    )

    fee_type = db.Column(
        db.String(50),
        nullable=False
    )

    amount = db.Column(
        db.Float,
        nullable=False
    )