from flask import Blueprint, render_template, request, jsonify, send_file
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy import func, extract
import io
import csv
import logging
from datetime import datetime, date

from app.utils.utilities import _get_previous_balance
from app.models.core import UserModule
from app.extensions import db, limiter
from app.models.people import Student, Staff
from app.models.finance import Invoice, InvoiceItem, Payment, Receipt, Expenses, FeeStructure
from app.models.academic_structure import Term, AcademicYear, AcademicConfig, Class
from app.models.user import User
from app.core.rate_limit import (
    READ_LIMIT, WRITE_LIMIT, PAYMENT_LIMIT,
    SEARCH_LIMIT, EXPORT_LIMIT,
)

logger = logging.getLogger(__name__)

finance_bp = Blueprint("finance", __name__)

PER_PAGE = 20


# =====================================================
# HELPERS
# =====================================================

def staff_required():
    claims = get_jwt()
    if claims.get("role") != "staff":
        return jsonify({"message": "Unauthorized"}), 403
    return None


def get_school_id():
    return get_jwt().get("school_id")


def _active_term(school_id):
    return Term.query.filter_by(school_id=school_id, status="active").first()


def _generate_receipt_number(school_id):
    count = db.session.query(func.count(Receipt.id)).join(
        Payment, Payment.id == Receipt.payment_id
    ).filter(Payment.school_id == school_id).scalar() or 0
    return f"RCP-{school_id}-{count + 1:05d}"


def _build_term_filter(school_id, year_name=None, term_name=None):
    query = Term.query.filter_by(school_id=school_id)
    if term_name:
        query = query.filter_by(name=term_name)
    if year_name:
        year = AcademicYear.query.filter_by(name=year_name).first()
        if year:
            query = query.filter_by(academic_year_id=year.id)
    return [t.id for t in query.all()]


def _pagination_meta(page, per_page, total):
    return {
        "page":     page,
        "per_page": per_page,
        "total":    total,
        "pages":    max(1, -(-total // per_page)),
    }


# =====================================================
# PAGE ROUTES
# =====================================================

@finance_bp.route("/finance/fees-collection")
@jwt_required()
@limiter.limit(READ_LIMIT)
def fees_collection_page():
    guard = staff_required()
    if guard:
        return guard
    claims  = get_jwt()
    user_id = claims.get("sub")
    modules = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]
    return render_template("modules/finance/fees_collection.html", modules=modules)


@finance_bp.route("/finance/expense-tracking")
@jwt_required()
@limiter.limit(READ_LIMIT)
def expense_tracking_page():
    guard = staff_required()
    if guard:
        return guard
    claims  = get_jwt()
    user_id = claims.get("sub")
    modules = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]
    return render_template("modules/finance/expense_tracking.html", modules=modules)


@finance_bp.route("/finance/financial-reports")
@jwt_required()
@limiter.limit(READ_LIMIT)
def financial_reports_page():
    guard = staff_required()
    if guard:
        return guard
    claims  = get_jwt()
    user_id = claims.get("sub")
    modules = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]
    return render_template("modules/finance/financial_reports.html", modules=modules)


# =====================================================
# FEES COLLECTION — CLASSES
# =====================================================

@finance_bp.route("/finance/api/fees/classes")
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_classes():
    guard = staff_required()
    if guard:
        return guard
    school_id = get_school_id()
    try:
        classes = Class.query.filter_by(school_id=school_id).order_by(Class.name).all()
        return jsonify({"classes": [{"id": c.id, "name": c.name} for c in classes]})
    except Exception:
        logger.exception("get_classes failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load classes."}), 500


# =====================================================
# FEES COLLECTION — STUDENTS  (paginated)
# =====================================================

@finance_bp.route("/finance/api/fees/students")
@jwt_required()
@limiter.limit(SEARCH_LIMIT)
def get_students_fees():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    search    = request.args.get("search", "").strip()
    class_id  = request.args.get("class_id", "")
    page      = max(1, int(request.args.get("page", 1)))

    try:
        term = _active_term(school_id)
        if not term:
            return jsonify({
                "students":   [],
                "message":    "No active term",
                "pagination": _pagination_meta(1, PER_PAGE, 0),
            })

        query = Student.query.filter_by(school_id=school_id)

        if class_id:
            query = query.filter_by(class_id=int(class_id))

        if search:
            like = f"%{search}%"
            query = query.filter(db.or_(
                Student.first_name.ilike(like),
                Student.last_name.ilike(like),
                Student.student_code.ilike(like),
            ))

        total_students = query.count()
        students_page  = query.order_by(Student.first_name).offset(
            (page - 1) * PER_PAGE
        ).limit(PER_PAGE).all()

        result = []

        for s in students_page:
            invoice = Invoice.query.filter_by(
                school_id=school_id,
                student_id=s.id,
                term_id=term.id,
            ).first()

            # ── AUTO-GENERATE INVOICE IF MISSING ─────────────────
            if not invoice:
                fee = FeeStructure.query.filter_by(
                    school_id=school_id,
                    class_id=s.class_id,
                    term_id=term.id,
                    student_type=getattr(s, "student_type", "day"),
                ).first()

                if fee:
                    try:
                        carried_balance = _get_previous_balance(school_id, s.id, term.id)
                        total_amount    = fee.total_amount + carried_balance

                        invoice = Invoice(
                            school_id=school_id,
                            student_id=s.id,
                            term_id=term.id,
                            year_id=term.academic_year_id,
                            total_amount=total_amount,
                        )
                        db.session.add(invoice)
                        db.session.flush()

                        for item in fee.items:
                            db.session.add(InvoiceItem(
                                invoice_id=invoice.id,
                                fee_type=item.fee_type,
                                amount=item.amount,
                            ))

                        if carried_balance > 0:
                            db.session.add(InvoiceItem(
                                invoice_id=invoice.id,
                                fee_type="Carried Forward Balance",
                                amount=carried_balance,
                            ))

                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                        logger.exception(
                            "Auto-invoice generation failed | student_id=%s school_id=%s",
                            s.id, school_id,
                        )
                        invoice = None

            if invoice:
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

        return jsonify({
            "students":   result,
            "term":       term.name,
            "pagination": _pagination_meta(page, PER_PAGE, total_students),
        })

    except Exception:
        logger.exception("get_students_fees failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load student fee data."}), 500


# =====================================================
# FEES COLLECTION — INVOICE DETAIL
# =====================================================

@finance_bp.route("/finance/api/fees/invoice/<int:invoice_id>")
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_invoice(invoice_id):
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    try:
        invoice = Invoice.query.filter_by(id=invoice_id, school_id=school_id).first_or_404()

        student = Student.query.get(invoice.student_id)
        term    = Term.query.get(invoice.term_id)
        year    = AcademicYear.query.get(invoice.year_id)
        cls     = Class.query.get(student.class_id) if student else None

        return jsonify({
            "invoice_id":   invoice.id,
            "student_name": f"{student.first_name} {student.last_name}" if student else "",
            "class_name":   cls.name if cls else "",
            "term_name":    term.name if term else "",
            "year_name":    year.name if year else "",
            "total_amount": invoice.total_amount,
            "amount_paid":  invoice.amount_paid,
            "balance":      invoice.balance,
            "items":        [{"fee_type": i.fee_type, "amount": i.amount} for i in invoice.items],
        })
    except Exception:
        logger.exception("get_invoice failed | invoice_id=%s school_id=%s", invoice_id, school_id)
        return jsonify({"error": "Failed to load invoice."}), 500


# =====================================================
# FEES COLLECTION — RECORD PAYMENT
# =====================================================

@finance_bp.route("/finance/api/fees/pay", methods=["POST"])
@jwt_required()
@limiter.limit(PAYMENT_LIMIT)
def record_payment():
    guard = staff_required()
    if guard:
        return guard

    school_id  = get_school_id()
    data       = request.get_json()
    invoice_id = data.get("invoice_id")
    amount     = float(data.get("amount", 0))
    method     = data.get("method", "cash")
    reference  = data.get("reference", "").strip()

    if not invoice_id or amount <= 0:
        return jsonify({"error": "Invalid invoice or amount"}), 400

    try:
        invoice = Invoice.query.filter_by(id=invoice_id, school_id=school_id).first_or_404()

        term = Term.query.get(invoice.term_id)
        if term and term.status != "active":
            return jsonify({"error": "Cannot record payments for a locked term"}), 400

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
            status="completed",
        )
        db.session.add(payment)
        db.session.flush()

        receipt = Receipt(
            payment_id=payment.id,
            receipt_number=_generate_receipt_number(school_id),
        )
        db.session.add(receipt)
        db.session.commit()

        return jsonify({
            "message":        "Payment recorded",
            "receipt_number": receipt.receipt_number,
            "new_balance":    invoice.balance,
        })

    except Exception:
        db.session.rollback()
        logger.exception(
            "record_payment failed | invoice_id=%s school_id=%s", invoice_id, school_id
        )
        return jsonify({"error": "Failed to record payment. Please try again."}), 500


# =====================================================
# FEES COLLECTION — PAYMENT HISTORY  (paginated)
# =====================================================

@finance_bp.route("/finance/api/fees/history/<int:student_id>")
@jwt_required()
@limiter.limit(READ_LIMIT)
def payment_history(student_id):
    guard = staff_required()
    if guard:
        return guard

    school_id   = get_school_id()
    year_filter = request.args.get("year", "")
    term_filter = request.args.get("term", "")
    page        = max(1, int(request.args.get("page", 1)))

    try:
        Student.query.filter_by(id=student_id, school_id=school_id).first_or_404()

        payments = Payment.query.filter_by(
            school_id=school_id, student_id=student_id, status="completed"
        ).order_by(Payment.created_at.desc()).all()

        rows = []
        for p in payments:
            invoice   = Invoice.query.get(p.invoice_id)
            term      = Term.query.get(invoice.term_id) if invoice else None
            year      = AcademicYear.query.get(invoice.year_id) if invoice else None
            receipt   = p.receipt
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

        total      = len(rows)
        start      = (page - 1) * PER_PAGE
        paged_rows = rows[start: start + PER_PAGE]

        active_term     = _active_term(school_id)
        current_invoice = None
        if active_term:
            current_invoice = Invoice.query.filter_by(
                school_id=school_id, student_id=student_id, term_id=active_term.id
            ).first()

        return jsonify({
            "payments": paged_rows,
            "summary": {
                "total_paid": sum(r["amount"] for r in rows),
                "balance":    current_invoice.balance if current_invoice else 0,
            },
            "pagination": _pagination_meta(page, PER_PAGE, total),
        })

    except Exception:
        logger.exception(
            "payment_history failed | student_id=%s school_id=%s", student_id, school_id
        )
        return jsonify({"error": "Failed to load payment history."}), 500


# =====================================================
# EXPENSE TRACKING — TERM INFO
# =====================================================

@finance_bp.route("/finance/api/expenses/term-info")
@jwt_required()
@limiter.limit(READ_LIMIT)
def expense_term_info():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    try:
        term = Term.query.filter_by(school_id=school_id, status="active").first()
        if not term:
            term = Term.query.filter_by(
                school_id=school_id, status="locked"
            ).order_by(Term.id.desc()).first()

        if not term:
            return jsonify({
                "status": "none", "term_name": None,
                "year_name": None, "term_id": None,
            })

        year = AcademicYear.query.get(term.academic_year_id)
        return jsonify({
            "status":    term.status,
            "term_name": term.name,
            "year_name": year.name if year else "",
            "term_id":   term.id,
        })
    except Exception:
        logger.exception("expense_term_info failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load term info."}), 500


# =====================================================
# EXPENSE TRACKING — KPIs
# =====================================================

@finance_bp.route("/finance/api/expenses/kpis")
@jwt_required()
@limiter.limit(READ_LIMIT)
def expense_kpis():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    try:
        active  = _active_term(school_id)
        term_id = active.id if active else None
        today   = date.today()

        this_term = float(
            db.session.query(func.sum(Expenses.amount)).filter_by(
                school_id=school_id, term_id=term_id
            ).scalar() or 0
        ) if term_id else 0.0

        this_month = float(
            db.session.query(func.sum(Expenses.amount)).filter(
                Expenses.school_id == school_id,
                extract("month", Expenses.date) == today.month,
                extract("year",  Expenses.date) == today.year,
            ).scalar() or 0
        )

        pending = float(
            db.session.query(func.sum(Expenses.amount)).filter_by(
                school_id=school_id, term_id=term_id, status="Pending"
            ).scalar() or 0
        ) if term_id else 0.0

        count = db.session.query(func.count(Expenses.id)).filter_by(
            school_id=school_id, term_id=term_id
        ).scalar() or 0

        return jsonify({
            "this_term":  this_term,
            "this_month": this_month,
            "pending":    pending,
            "count":      count,
        })
    except Exception:
        logger.exception("expense_kpis failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load expense KPIs."}), 500


# =====================================================
# EXPENSE TRACKING — YEAR LIST
# =====================================================

@finance_bp.route("/finance/api/expenses/years")
@jwt_required()
@limiter.limit(READ_LIMIT)
def expense_years():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    try:
        terms    = Term.query.filter_by(school_id=school_id).all()
        year_ids = list({t.academic_year_id for t in terms})
        years    = AcademicYear.query.filter(
            AcademicYear.id.in_(year_ids)
        ).order_by(AcademicYear.name.desc()).all()
        return jsonify({"years": [y.name for y in years]})
    except Exception:
        logger.exception("expense_years failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load years."}), 500


# =====================================================
# EXPENSE TRACKING — LIST  (paginated)
# =====================================================

@finance_bp.route("/finance/api/expenses", methods=["GET"])
@jwt_required()
@limiter.limit(SEARCH_LIMIT)
def get_expenses():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    year_f    = request.args.get("year", "")
    term_f    = request.args.get("term", "")
    cat_f     = request.args.get("category", "")
    page      = max(1, int(request.args.get("page", 1)))

    try:
        query = Expenses.query.filter_by(school_id=school_id)

        if year_f or term_f:
            term_ids = _build_term_filter(school_id, year_f or None, term_f or None)
            if term_ids:
                query = query.filter(Expenses.term_id.in_(term_ids))
            else:
                return jsonify({
                    "expenses":   [],
                    "pagination": _pagination_meta(1, PER_PAGE, 0),
                })

        if cat_f:
            query = query.filter_by(category=cat_f)

        total         = query.count()
        expenses_page = query.order_by(Expenses.date.desc()).offset(
            (page - 1) * PER_PAGE
        ).limit(PER_PAGE).all()

        active         = _active_term(school_id)
        active_term_id = active.id if active else None

        result = []
        for e in expenses_page:
            term = Term.query.get(e.term_id) if e.term_id else None
            year = AcademicYear.query.get(e.year_id) if e.year_id else None
            result.append({
                "id":             e.id,
                "title":          e.title,
                "category":       e.category,
                "amount":         e.amount,
                "date":           str(e.date),
                "payment_method": e.payment_method,
                "status":         e.status,
                "term_name":      term.name if term else "",
                "year_name":      year.name if year else "",
                "can_delete":     (e.term_id == active_term_id),
            })

        return jsonify({
            "expenses":   result,
            "pagination": _pagination_meta(page, PER_PAGE, total),
        })
    except Exception:
        logger.exception("get_expenses failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load expenses."}), 500


# =====================================================
# EXPENSE TRACKING — CREATE
# =====================================================

@finance_bp.route("/finance/api/expenses", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def create_expense():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    term      = _active_term(school_id)

    if not term:
        return jsonify({"error": "No active term. Cannot add expenses."}), 400
    if term.status != "active":
        return jsonify({"error": "Term is locked. Cannot add expenses."}), 400

    data = request.get_json()

    try:
        expense_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return jsonify({"error": "Invalid date format"}), 400

    title  = data.get("title", "").strip()
    amount = float(data.get("amount", 0))

    if not title or amount <= 0:
        return jsonify({"error": "Title and amount are required"}), 400

    try:
        expense = Expenses(
            school_id=school_id,
            term_id=term.id,
            year_id=term.academic_year_id,
            title=title,
            category=data.get("category", "Other"),
            amount=amount,
            date=expense_date,
            status=data.get("status", "Approved"),
            payment_method=data.get("payment_method", "Cash"),
        )

        db.session.add(expense)
        db.session.commit()
        return jsonify({"message": "Expense saved"})
    except Exception:
        db.session.rollback()
        logger.exception("create_expense failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to save expense. Please try again."}), 500


# =====================================================
# EXPENSE TRACKING — DELETE
# =====================================================

@finance_bp.route("/finance/api/expenses/<int:expense_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_expense(expense_id):
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    try:
        expense = Expenses.query.filter_by(id=expense_id, school_id=school_id).first_or_404()

        term = Term.query.get(expense.term_id) if expense.term_id else None
        if term and term.status != "active":
            return jsonify({"error": "Cannot delete expenses from a locked term"}), 400

        db.session.delete(expense)
        db.session.commit()
        return jsonify({"message": "Expense deleted"})
    except Exception:
        db.session.rollback()
        logger.exception("delete_expense failed | expense_id=%s school_id=%s", expense_id, school_id)
        return jsonify({"error": "Failed to delete expense. Please try again."}), 500


# =====================================================
# FINANCIAL REPORTS — SUMMARY KPIs
# =====================================================

def _get_summary(school_id, year_f, term_f):
    term_ids = _build_term_filter(school_id, year_f or None, term_f or None) if (year_f or term_f) else None

    income_q = db.session.query(func.sum(Payment.amount)).filter_by(
        school_id=school_id, status="completed"
    )
    if term_ids is not None:
        income_q = income_q.join(Invoice, Invoice.id == Payment.invoice_id).filter(
            Invoice.term_id.in_(term_ids)
        )
    income = float(income_q.scalar() or 0)

    exp_q = db.session.query(func.sum(Expenses.amount)).filter_by(school_id=school_id)
    if term_ids is not None:
        exp_q = exp_q.filter(Expenses.term_id.in_(term_ids))
    expenses = float(exp_q.scalar() or 0)

    inv_q = db.session.query(func.sum(Invoice.total_amount)).filter_by(school_id=school_id)
    if term_ids is not None:
        inv_q = inv_q.filter(Invoice.term_id.in_(term_ids))
    outstanding = max(float(inv_q.scalar() or 0) - income, 0)

    return income, expenses, outstanding


@finance_bp.route("/finance/api/reports/summary")
@jwt_required()
@limiter.limit(READ_LIMIT)
def reports_summary():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    year_f    = request.args.get("year", "")
    term_f    = request.args.get("term", "")

    try:
        income, expenses, outstanding = _get_summary(school_id, year_f, term_f)
        return jsonify({"income": income, "expenses": expenses, "outstanding": outstanding})
    except Exception:
        logger.exception("reports_summary failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load financial summary."}), 500


# =====================================================
# FINANCIAL REPORTS — CHART DATA
# =====================================================

@finance_bp.route("/finance/api/reports/chart-data")
@jwt_required()
@limiter.limit(READ_LIMIT)
def reports_chart_data():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_school_id()
    year_f    = request.args.get("year", "")
    term_f    = request.args.get("term", "")

    try:
        term_ids = _build_term_filter(school_id, year_f or None, term_f or None) if (year_f or term_f) else None

        today     = date.today()
        months    = []
        m_income  = []
        m_expense = []

        for i in range(11, -1, -1):
            month_num = ((today.month - 1 - i) % 12) + 1
            year_num  = today.year - ((today.month - 1 - i) // 12 + (1 if (today.month - 1 - i) < 0 else 0))
            months.append(date(year_num, month_num, 1).strftime("%b %Y"))

            inc_q = db.session.query(func.sum(Payment.amount)).filter(
                Payment.school_id == school_id,
                Payment.status    == "completed",
                extract("month", Payment.created_at) == month_num,
                extract("year",  Payment.created_at) == year_num,
            )
            if term_ids is not None:
                inc_q = inc_q.join(Invoice, Invoice.id == Payment.invoice_id).filter(
                    Invoice.term_id.in_(term_ids)
                )
            m_income.append(float(inc_q.scalar() or 0))

            exp_q = db.session.query(func.sum(Expenses.amount)).filter(
                Expenses.school_id == school_id,
                extract("month", Expenses.date) == month_num,
                extract("year",  Expenses.date) == year_num,
            )
            if term_ids is not None:
                exp_q = exp_q.filter(Expenses.term_id.in_(term_ids))
            m_expense.append(float(exp_q.scalar() or 0))

        categories  = ["Salaries", "Utilities", "Supplies", "Maintenance", "Transport", "Other"]
        cat_amounts = []
        for cat in categories:
            q = db.session.query(func.sum(Expenses.amount)).filter_by(
                school_id=school_id, category=cat
            )
            if term_ids is not None:
                q = q.filter(Expenses.term_id.in_(term_ids))
            cat_amounts.append(float(q.scalar() or 0))

        paired      = [(l, a) for l, a in zip(categories, cat_amounts) if a > 0]
        cat_labels  = [p[0] for p in paired]
        cat_amounts = [p[1] for p in paired]

        return jsonify({
            "monthly":    {"labels": months, "income": m_income, "expenses": m_expense},
            "categories": {"labels": cat_labels, "amounts": cat_amounts},
        })
    except Exception:
        logger.exception("reports_chart_data failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load chart data."}), 500


# =====================================================
# FINANCIAL REPORTS — DOWNLOAD CSV
# =====================================================

@finance_bp.route("/finance/api/reports/download")
@jwt_required()
@limiter.limit(EXPORT_LIMIT)
def download_report():
    guard = staff_required()
    if guard:
        return guard

    school_id   = get_school_id()
    report_type = request.args.get("type", "income")
    period      = request.args.get("period", "termly")
    year_f      = request.args.get("year", "")
    term_f      = request.args.get("term", "")

    try:
        buf    = io.StringIO()
        writer = csv.writer(buf)

        if report_type == "income" and period == "termly":
            writer.writerow(["Term", "Year", "Total Invoiced", "Amount Collected", "Outstanding"])
            for t in Term.query.filter_by(school_id=school_id).order_by(Term.id).all():
                yr = AcademicYear.query.get(t.academic_year_id)
                if year_f and (not yr or yr.name != year_f):
                    continue
                if term_f and t.name != term_f:
                    continue
                invoiced  = db.session.query(func.sum(Invoice.total_amount)).filter_by(
                    school_id=school_id, term_id=t.id).scalar() or 0
                collected = db.session.query(func.sum(Payment.amount)).join(
                    Invoice, Invoice.id == Payment.invoice_id
                ).filter(
                    Invoice.school_id == school_id,
                    Invoice.term_id   == t.id,
                    Payment.status    == "completed",
                ).scalar() or 0
                writer.writerow([
                    t.name, yr.name if yr else "",
                    f"{invoiced:,.0f}", f"{collected:,.0f}",
                    f"{max(float(invoiced) - float(collected), 0):,.0f}",
                ])

        elif report_type == "income" and period == "monthly":
            writer.writerow(["Month", "Year", "Amount Collected"])
            today = date.today()
            for i in range(11, -1, -1):
                mn = ((today.month - 1 - i) % 12) + 1
                yn = today.year - ((today.month - 1 - i) // 12 + (1 if (today.month - 1 - i) < 0 else 0))
                collected = db.session.query(func.sum(Payment.amount)).filter(
                    Payment.school_id == school_id,
                    Payment.status    == "completed",
                    extract("month", Payment.created_at) == mn,
                    extract("year",  Payment.created_at) == yn,
                ).scalar() or 0
                writer.writerow([date(yn, mn, 1).strftime("%B"), yn, f"{collected:,.0f}"])

        elif report_type == "expenses" and period == "termly":
            writer.writerow(["Term", "Year", "Title", "Category", "Amount", "Date", "Method", "Status"])
            term_ids = _build_term_filter(school_id, year_f or None, term_f or None) if (year_f or term_f) else None
            q = Expenses.query.filter_by(school_id=school_id)
            if term_ids is not None:
                q = q.filter(Expenses.term_id.in_(term_ids))
            for e in q.order_by(Expenses.date).all():
                t  = Term.query.get(e.term_id) if e.term_id else None
                yr = AcademicYear.query.get(e.year_id) if e.year_id else None
                writer.writerow([
                    t.name if t else "", yr.name if yr else "",
                    e.title, e.category, f"{e.amount:,.0f}",
                    str(e.date), e.payment_method or "", e.status or "",
                ])

        elif report_type == "expenses" and period == "monthly":
            writer.writerow(["Month", "Year", "Category", "Total Amount"])
            today = date.today()
            cats  = ["Salaries", "Utilities", "Supplies", "Maintenance", "Transport", "Other"]
            for i in range(11, -1, -1):
                mn = ((today.month - 1 - i) % 12) + 1
                yn = today.year - ((today.month - 1 - i) // 12 + (1 if (today.month - 1 - i) < 0 else 0))
                for cat in cats:
                    amt = db.session.query(func.sum(Expenses.amount)).filter(
                        Expenses.school_id == school_id,
                        Expenses.category  == cat,
                        extract("month", Expenses.date) == mn,
                        extract("year",  Expenses.date) == yn,
                    ).scalar() or 0
                    if amt:
                        writer.writerow([date(yn, mn, 1).strftime("%B"), yn, cat, f"{amt:,.0f}"])

        elif report_type == "profit" and period == "termly":
            writer.writerow(["Term", "Year", "Income", "Expenses", "Net Balance", "Status"])
            for t in Term.query.filter_by(school_id=school_id).order_by(Term.id).all():
                yr = AcademicYear.query.get(t.academic_year_id)
                if year_f and (not yr or yr.name != year_f):
                    continue
                if term_f and t.name != term_f:
                    continue
                income = db.session.query(func.sum(Payment.amount)).join(
                    Invoice, Invoice.id == Payment.invoice_id
                ).filter(
                    Invoice.school_id == school_id,
                    Invoice.term_id   == t.id,
                    Payment.status    == "completed",
                ).scalar() or 0
                expenses = db.session.query(func.sum(Expenses.amount)).filter_by(
                    school_id=school_id, term_id=t.id
                ).scalar() or 0
                net = float(income) - float(expenses)
                writer.writerow([
                    t.name, yr.name if yr else "",
                    f"{income:,.0f}", f"{expenses:,.0f}",
                    f"{net:,.0f}", "Surplus" if net >= 0 else "Deficit",
                ])

        elif report_type == "profit" and period == "monthly":
            writer.writerow(["Month", "Year", "Income", "Expenses", "Net Balance", "Status"])
            today = date.today()
            for i in range(11, -1, -1):
                mn = ((today.month - 1 - i) % 12) + 1
                yn = today.year - ((today.month - 1 - i) // 12 + (1 if (today.month - 1 - i) < 0 else 0))
                income = db.session.query(func.sum(Payment.amount)).filter(
                    Payment.school_id == school_id,
                    Payment.status    == "completed",
                    extract("month", Payment.created_at) == mn,
                    extract("year",  Payment.created_at) == yn,
                ).scalar() or 0
                expenses = db.session.query(func.sum(Expenses.amount)).filter(
                    Expenses.school_id == school_id,
                    extract("month", Expenses.date) == mn,
                    extract("year",  Expenses.date) == yn,
                ).scalar() or 0
                net = float(income) - float(expenses)
                writer.writerow([
                    date(yn, mn, 1).strftime("%B"), yn,
                    f"{income:,.0f}", f"{expenses:,.0f}",
                    f"{net:,.0f}", "Surplus" if net >= 0 else "Deficit",
                ])

        else:
            return jsonify({"error": "Invalid report type or period"}), 400

        buf.seek(0)
        return send_file(
            io.BytesIO(buf.getvalue().encode("utf-8")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"{report_type}_{period}_report.csv",
        )

    except Exception:
        logger.exception(
            "download_report failed | school_id=%s type=%s period=%s",
            school_id, report_type, period,
        )
        return jsonify({"error": "Failed to generate report. Please try again."}), 500