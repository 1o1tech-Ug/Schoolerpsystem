from app.models.core import School, Subscription
from app.models.people import Student, Staff
from app.models.finance import (
    Invoice,
    InvoiceItem,
    Payment,
    StudentFeeStructure,
    StudentFeeItem,
)
from app.extensions import db


# ==========================================
# STAFF CODE GENERATOR
# ==========================================
def generate_staff_code(school_id):

    last_code = (
        db.session.query(Staff.staff_code)
        .filter_by(school_id=school_id)
        .order_by(Staff.id.desc())
        .first()
    )

    if not last_code or not last_code[0]:
        return "STF-001"

    try:
        number = int(last_code[0].split("-")[1])
    except (ValueError, IndexError, TypeError):
        number = 0

    return f"STF-{number + 1:03d}"


# ==========================================
# PREVIOUS BALANCE
# ==========================================
def _get_previous_balance(school_id, student_id, current_term_id):
    """
    Find the student's most recently created invoice that is NOT
    for the current term and return its unpaid balance.

    Returns 0.0 if:
        - no previous invoice exists
        - the previous invoice is fully paid
        - the previous invoice has a negative balance

    The previous invoice is determined by Invoice.id descending.
    """

    prev_invoice = (
        Invoice.query
        .filter(
            Invoice.school_id == school_id,
            Invoice.student_id == student_id,
            Invoice.term_id != current_term_id,
        )
        .order_by(Invoice.id.desc())
        .first()
    )

    if not prev_invoice:
        return 0.0

    balance = prev_invoice.balance

    return max(float(balance or 0), 0.0)


# ==========================================
# GENERATE INVOICES FOR A TERM
# ==========================================
def generate_invoices_for_term(school_id, term):
    """
    Generate invoices for all students who have a
    StudentFeeStructure for the supplied term.

    Fee structures are now assigned per student.

    Flow:

        Student
            ↓
        StudentFeeStructure
            ↓
        StudentFeeItem
            ↓
        Invoice
            ↓
        InvoiceItem

    Any unpaid balance from the student's previous invoice
    is carried forward into the new invoice.

    Existing invoices are skipped so this function is safe
    to run more than once.
    """

    students = Student.query.filter_by(
        school_id=school_id
    ).all()

    created_count = 0

    for student in students:

        # ------------------------------------------
        # Find this student's fee structure
        # for the current term
        # ------------------------------------------
        student_fee = (
            StudentFeeStructure.query
            .filter_by(
                school_id=school_id,
                student_id=student.id,
                term_id=term.id,
            )
            .first()
        )

        # No fee assigned to this student
        # for this term.
        if not student_fee:
            continue

        # ------------------------------------------
        # Avoid duplicate invoices
        # ------------------------------------------
        existing = (
            Invoice.query
            .filter_by(
                school_id=school_id,
                student_id=student.id,
                term_id=term.id,
            )
            .first()
        )

        if existing:
            continue

        # ------------------------------------------
        # Carry forward previous unpaid balance
        # ------------------------------------------
        carried_balance = _get_previous_balance(
            school_id,
            student.id,
            term.id,
        )

        total_amount = (
            float(student_fee.total_amount or 0)
            + carried_balance
        )

        # ------------------------------------------
        # Create invoice
        # ------------------------------------------
        invoice = Invoice(
            school_id=school_id,
            student_id=student.id,
            term_id=term.id,
            year_id=term.academic_year_id,
            total_amount=total_amount,
        )

        db.session.add(invoice)
        db.session.flush()

        # ------------------------------------------
        # Copy StudentFeeItems → InvoiceItems
        # ------------------------------------------
        fee_items = (
            StudentFeeItem.query
            .filter_by(
                student_fee_structure_id=student_fee.id
            )
            .all()
        )

        for item in fee_items:

            db.session.add(
                InvoiceItem(
                    invoice_id=invoice.id,
                    fee_type=item.fee_type,
                    amount=item.amount,
                )
            )

        # ------------------------------------------
        # Add carried balance as separate line item
        # ------------------------------------------
        if carried_balance > 0:

            db.session.add(
                InvoiceItem(
                    invoice_id=invoice.id,
                    fee_type="Carried Forward Balance",
                    amount=carried_balance,
                )
            )

        created_count += 1

    db.session.commit()

    return created_count


# ==========================================
# SUBSCRIPTION LIMIT CHECKER
# ==========================================
def check_student_limit(school_id):

    subscription = (
        Subscription.query
        .filter_by(school_id=school_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )

    if not subscription:
        return "School subscription not found"

    plan = (subscription.payment_plan or "").lower()

    limits = {
        "basic": 550,
        "standard": 1600,
        "premium": 2100,
    }

    if plan not in limits:
        return "Invalid subscription plan"

    current_students = (
        Student.query
        .filter_by(school_id=school_id)
        .count()
    )

    max_students = limits[plan]

    if current_students >= max_students:
        return (
            f"{plan.capitalize()} plan limit reached. "
            f"Maximum allowed students is {max_students}."
        )

    return None


# ==========================================
# SCHOOL CODE GENERATOR
# ==========================================
def generate_school_code():
    """
    Generates a unique school code based on the
    last School ID.

    Format:
        SCH-001
        SCH-002
        SCH-103
    """

    last_school = (
        School.query
        .order_by(School.id.desc())
        .first()
    )

    if last_school:
        next_id = last_school.id + 1
    else:
        next_id = 1

    return f"SCH-{next_id:03d}"