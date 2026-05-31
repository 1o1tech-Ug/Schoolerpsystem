from app.models.core import School, Subscription
from app.models.people import Student, Staff
from app.models.finance import Invoice, InvoiceItem, FeeStructure, Payment
from app.extensions import db
from sqlalchemy import func


def generate_staff_code(school_id):

    last_code = db.session.query(Staff.staff_code)\
        .filter_by(school_id=school_id)\
        .order_by(Staff.id.desc())\
        .first()

    if not last_code or not last_code[0]:
        return "STF-001"

    try:
        number = int(last_code[0].split("-")[1])
    except:
        number = 0

    return f"STF-{number + 1:03d}"


def _get_previous_balance(school_id, student_id, current_term_id):
    """
    Find the student's most recently created invoice that is NOT
    for the current term and return the unpaid balance on it.
    Returns 0.0 if no previous invoice exists or it is fully paid.
    """
    prev_invoice = (
        Invoice.query
        .filter(
            Invoice.school_id == school_id,
            Invoice.student_id == student_id,
            Invoice.term_id != current_term_id
        )
        .order_by(Invoice.id.desc())   # most recent previous term
        .first()
    )

    if not prev_invoice:
        return 0.0

    balance = prev_invoice.balance   # uses the @property on Invoice model
    return max(balance, 0.0)         # never carry a negative


def generate_invoices_for_term(school_id, term):

    students = Student.query.filter_by(
        school_id=school_id
    ).all()

    created_count = 0

    for student in students:

        # find fee structure for this student's class + type
        fee = FeeStructure.query.filter_by(
            school_id=school_id,
            class_id=student.class_id,
            term_id=term.id,
            student_type=getattr(student, "student_type", "day")
        ).first()

        if not fee:
            continue

        # avoid duplicates
        existing = Invoice.query.filter_by(
            school_id=school_id,
            student_id=student.id,
            term_id=term.id
        ).first()

        if existing:
            continue

        # ── CARRY FORWARD any unpaid balance from previous term ──
        carried_balance = _get_previous_balance(
            school_id, student.id, term.id
        )

        total_amount = fee.total_amount + carried_balance

        invoice = Invoice(
            school_id=school_id,
            student_id=student.id,
            term_id=term.id,
            year_id=term.academic_year_id,
            total_amount=total_amount
        )

        db.session.add(invoice)
        db.session.flush()

        # copy current term fee items
        for item in fee.items:
            db.session.add(InvoiceItem(
                invoice_id=invoice.id,
                fee_type=item.fee_type,
                amount=item.amount
            ))

        # add carried balance as its own line item so it's visible
        if carried_balance > 0:
            db.session.add(InvoiceItem(
                invoice_id=invoice.id,
                fee_type="Carried Forward Balance",
                amount=carried_balance
            ))

        created_count += 1

    db.session.commit()

    return created_count


# ==========================================
# SUBSCRIPTION LIMIT CHECKER
# ==========================================
def check_student_limit(school_id):

    subscription = Subscription.query.filter_by(
        school_id=school_id
    ).order_by(Subscription.created_at.desc()).first()

    if not subscription:
        return "School subscription not found"

    plan = (subscription.payment_plan or "").lower()

    limits = {
        "basic": 550,
        "standard": 1600,
        "premium": 2100
    }

    if plan not in limits:
        return "Invalid subscription plan"

    current_students = Student.query.filter_by(
        school_id=school_id
    ).count()

    max_students = limits[plan]

    if current_students >= max_students:
        return (
            f"{plan.capitalize()} plan limit reached. "
            f"Maximum allowed students is {max_students}."
        )

    return None


def generate_school_code():
    """
    Generates a unique school code based on the last school ID.
    Format: SCH-{ID}
    Example: SCH-001, SCH-002, SCH-103
    """

    last_school = School.query.order_by(School.id.desc()).first()

    if last_school:
        next_id = last_school.id + 1
    else:
        next_id = 1

    return f"SCH-{next_id:03d}"
