"""
app/apis/finance_api.py
========================
Finance blueprint — fee collection, invoices, payments, payment history.

CHANGES vs original:
  - Rate limits applied per endpoint sensitivity.
  - All except blocks log internally and return safe client messages.
    No str(e) ever reaches the client.
"""

import logging
from flask import Blueprint, render_template, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt
from functools import wraps
from sqlalchemy import func

from app.extensions import db, limiter
from app.models.people import Student
from app.models.finance import Invoice, Payment, Receipt, Expenses, FeeStructure
from app.models.academic_structure import Term, AcademicYear, AcademicConfig, Class
from app.models.user import User
from app.core.rate_limit import (
    READ_LIMIT, WRITE_LIMIT, PAYMENT_LIMIT, SEARCH_LIMIT,
)

logger = logging.getLogger(__name__)

finance_bp = Blueprint("finance", __name__)


# =====================================================
# HELPERS
# =====================================================

def finance_required(fn):
    """Allows admin role OR staff with 'finance' module."""
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        claims = get_jwt()
        role = claims.get("role")
        if role == "admin":
            return fn(*args, **kwargs)
        if role == "staff":
            modules = claims.get("modules", [])
            if "finance" in modules:
                return fn(*args, **kwargs)
        return jsonify({"error": "Finance access required"}), 403
    return wrapper


def get_school_id():
    return get_jwt().get("school_id")


def _active_term(school_id):
    return Term.query.filter_by(school_id=school_id, status="active").first()


def _generate_receipt_number(school_id):
    count = db.session.query(func.count(Receipt.id)).join(
        Payment, Payment.id == Receipt.payment_id
    ).filter(Payment.school_id == school_id).scalar() or 0
    return f"RCP-{school_id}-{count + 1:05d}"


# =====================================================
# PAGE ROUTE
# =====================================================

@finance_bp.route("/finance/fees-collection")
@finance_required
@limiter.limit(READ_LIMIT)
def fees_collection_page():
    return render_template("finance/fees_collection.html")


# =====================================================
# API: CLASSES (for filter dropdown)
# =====================================================

@finance_bp.route("/finance/api/fees/classes")
@finance_required
@limiter.limit(READ_LIMIT)
def get_classes():
    school_id = get_school_id()
    try:
        classes = Class.query.filter_by(school_id=school_id).order_by(Class.name).all()
        return jsonify({
            "classes": [{"id": c.id, "name": c.name} for c in classes]
        })
    except Exception:
        logger.exception("get_classes failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load classes."}), 500


# =====================================================
# API: STUDENTS WITH INVOICE SUMMARY (current term)
# =====================================================

@finance_bp.route("/finance/api/fees/students")
@finance_required
@limiter.limit(SEARCH_LIMIT)
def get_students_fees():
    school_id = get_school_id()
    search    = request.args.get("search", "").strip()
    class_id  = request.args.get("class_id", "")

    try:
        term = _active_term(school_id)
        if not term:
            return jsonify({"students": [], "message": "No active term"})

        query = Student.query.filter_by(school_id=school_id)

        if class_id:
            query = query.filter_by(class_id=int(class_id))

        if search:
            like = f"%{search}%"
            query = query.filter(
                db.or_(
                    Student.first_name.ilike(like),
                    Student.last_name.ilike(like),
                    Student.student_code.ilike(like)
                )
            )

        students = query.order_by(Student.first_name).all()

        result = []
        for s in students:
            invoice = Invoice.query.filter_by(
                school_id=school_id,
                student_id=s.id,
                term_id=term.id
            ).first()

            if not invoice:
                continue

            cls = Class.query.get(s.class_id)

            result.append({
                "student_id":   s.id,
                "invoice_id":   invoice.id,
                "student_code": s.student_code,
                "first_name":   s.first_name,
                "last_name":    s.last_name,
                "class_name":   cls.name if cls else "",
                "total_amount": invoice.total_amount,
                "amount_paid":  invoice.amount_paid,
                "balance":      invoice.balance,
            })

        return jsonify({"students": result, "term": term.name})

    except Exception:
        logger.exception("get_students_fees failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load student fee data."}), 500


# =====================================================
# API: INVOICE DETAIL
# =====================================================

@finance_bp.route("/finance/api/fees/invoice/<int:invoice_id>")
@finance_required
@limiter.limit(READ_LIMIT)
def get_invoice(invoice_id):
    school_id = get_school_id()

    try:
        invoice = Invoice.query.filter_by(
            id=invoice_id,
            school_id=school_id
        ).first_or_404()

        student = Student.query.get(invoice.student_id)
        term    = Term.query.get(invoice.term_id)
        year    = AcademicYear.query.get(invoice.year_id)
        cls     = Class.query.get(student.class_id) if student else None

        items = [
            {"fee_type": i.fee_type, "amount": i.amount}
            for i in invoice.items
        ]

        return jsonify({
            "invoice_id":   invoice.id,
            "student_name": f"{student.first_name} {student.last_name}" if student else "",
            "class_name":   cls.name if cls else "",
            "term_name":    term.name if term else "",
            "year_name":    year.name if year else "",
            "total_amount": invoice.total_amount,
            "amount_paid":  invoice.amount_paid,
            "balance":      invoice.balance,
            "items":        items,
        })

    except Exception:
        logger.exception("get_invoice failed | invoice_id=%s school_id=%s", invoice_id, school_id)
        return jsonify({"error": "Failed to load invoice."}), 500


# =====================================================
# API: RECORD PAYMENT
# =====================================================

@finance_bp.route("/finance/api/fees/pay", methods=["POST"])
@finance_required
@limiter.limit(PAYMENT_LIMIT)
def record_payment():
    school_id = get_school_id()
    data      = request.get_json()

    invoice_id = data.get("invoice_id")
    amount     = float(data.get("amount", 0))
    method     = data.get("method", "cash")
    reference  = data.get("reference", "").strip()

    if not invoice_id or amount <= 0:
        return jsonify({"error": "Invalid invoice or amount"}), 400

    try:
        invoice = Invoice.query.filter_by(
            id=invoice_id,
            school_id=school_id
        ).first_or_404()

        if invoice.balance <= 0:
            return jsonify({"error": "Invoice is already fully paid"}), 400

        if amount > invoice.balance:
            return jsonify({"error": f"Amount exceeds balance of {invoice.balance:,.0f}"}), 400

        payment = Payment(
            school_id=school_id,
            student_id=invoice.student_id,
            invoice_id=invoice.id,
            amount=amount,
            method=method,
            reference=reference or None,
            status="completed"
        )

        db.session.add(payment)
        db.session.flush()

        receipt = Receipt(
            payment_id=payment.id,
            receipt_number=_generate_receipt_number(school_id)
        )

        db.session.add(receipt)
        db.session.commit()

        return jsonify({
            "message":        "Payment recorded",
            "receipt_number": receipt.receipt_number,
            "new_balance":    invoice.balance
        })

    except Exception:
        db.session.rollback()
        logger.exception(
            "record_payment failed | invoice_id=%s school_id=%s", invoice_id, school_id
        )
        return jsonify({"error": "Failed to record payment. Please try again."}), 500


# =====================================================
# API: PAYMENT HISTORY (per student, filterable)
# =====================================================

@finance_bp.route("/finance/api/fees/history/<int:student_id>")
@finance_required
@limiter.limit(READ_LIMIT)
def payment_history(student_id):
    school_id   = get_school_id()
    year_filter = request.args.get("year", "")
    term_filter = request.args.get("term", "")

    try:
        student = Student.query.filter_by(
            id=student_id,
            school_id=school_id
        ).first_or_404()

        payments_q = Payment.query.filter_by(
            school_id=school_id,
            student_id=student_id,
            status="completed"
        ).order_by(Payment.created_at.desc())

        payments = payments_q.all()

        rows = []
        for p in payments:
            invoice = Invoice.query.get(p.invoice_id)
            term    = Term.query.get(invoice.term_id) if invoice else None
            year    = AcademicYear.query.get(invoice.year_id) if invoice else None
            receipt = p.receipt

            year_name = year.name if year else ""
            term_name = term.name if term else ""

            if year_filter and year_name != year_filter:
                continue
            if term_filter and term_name != term_filter:
                continue

            rows.append({
                "id":             p.id,
                "term_name":      term_name,
                "year":           year_name,
                "amount":         p.amount,
                "method":         p.method,
                "reference":      p.reference,
                "receipt_number": receipt.receipt_number if receipt else None,
                "created_at":     p.created_at.isoformat() if p.created_at else "",
            })

        active_term = _active_term(school_id)
        current_invoice = None
        if active_term:
            current_invoice = Invoice.query.filter_by(
                school_id=school_id,
                student_id=student_id,
                term_id=active_term.id
            ).first()

        total_paid = sum(r["amount"] for r in rows)
        balance    = current_invoice.balance if current_invoice else 0

        return jsonify({
            "payments": rows,
            "summary": {
                "total_paid": total_paid,
                "balance":    balance,
            }
        })

    except Exception:
        logger.exception(
            "payment_history failed | student_id=%s school_id=%s", student_id, school_id
        )
        return jsonify({"error": "Failed to load payment history."}), 500