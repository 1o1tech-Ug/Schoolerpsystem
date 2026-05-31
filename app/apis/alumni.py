from flask import Blueprint, render_template, jsonify, request, send_file, abort
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
import logging

from app.extensions import db, limiter
from app.models.core import School, UserModule
from app.models.people import Guardian, MedicalRecord, Document, Student, StudentAcademic
from app.models.user import User
from app.models.finance import Invoice, Payment, Receipt, InvoiceItem
from app.models.reportcards import ReportCard
from app.models.academic_structure import (
    Class, Stream, AcademicYear, Term,
    StudentStream, StudentEnrollment,
)
from app.core.rate_limit import READ_LIMIT, SEARCH_LIMIT

logger = logging.getLogger(__name__)

alumni_api = Blueprint(
    "alumni_api",
    __name__,
    url_prefix="/api/academics/alumni"
)


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def _staff_required(claims):
    role = claims.get("role")
    if role not in {"staff", "teacher"}:
        return None, (jsonify({"message": "Unauthorized"}), 403)

    school_id = claims.get("school_id")
    user_ = User.query.filter_by(
        school_id=school_id,
        role=role,
        id=claims.get("sub")
    ).first()

    if not user_:
        return None, (jsonify({"message": "User not found"}), 404)

    return user_, None


def _get_context(claims):
    school_id = claims.get("school_id")
    user_id   = claims.get("sub")
    modules   = [
        m.module_name
        for m in UserModule.query.filter_by(user_id=user_id).all()
    ]
    return school_id, user_id, modules


def _school_or_404(school_id):
    school = School.query.get(school_id)
    if not school:
        return None, (jsonify({"message": "School not found"}), 404)
    return school, None


STATUS_LABELS = {
    "active":         "Active",
    "promoted":       "Promoted",
    "level_complete": "Level Complete",
    "graduated":      "Graduated",
    "repeated":       "Repeated",
    "demoted":        "Demoted",
    "transferred":    "Transferred",
}


def _resolve_file_url(obj, url_field="firebase_url", local_field=None):
    url = getattr(obj, url_field, None)
    if url:
        return url
    if local_field:
        local = getattr(obj, local_field, None)
        if local:
            rel = local.lstrip("/")
            return f"/static/{rel}" if not rel.startswith("static/") else f"/{rel}"
    return None


def _serialize_document(doc):
    return {
        "id":            doc.id,
        "title":         doc.document_type,
        "document_type": doc.document_type,
        "file_url":      _resolve_file_url(doc, url_field="file_url"),
        "uploaded_at":   doc.uploaded_at.strftime("%d %b %Y") if doc.uploaded_at else None,
        "description":   None,
    }


def _serialize_report_card(rc):
    file_url = _resolve_file_url(rc, url_field="firebase_url", local_field="local_path")
    return {
        "id":            rc.id,
        "term_id":       rc.term_id,
        "exam_type":     rc.exam_type,
        "academic_year": rc.academic_year,
        "generated_at":  rc.generated_at.strftime("%d %b %Y %H:%M") if rc.generated_at else None,
        "status":        rc.status,
        "file_url":      file_url,
        "has_file":      file_url is not None,
    }


def _serialize_invoice(invoice):
    items = [
        {"id": item.id, "fee_type": item.fee_type, "amount": item.amount}
        for item in invoice.items
    ]

    payments = []
    for pmt in invoice.payments:
        receipt_number = None
        if pmt.receipt:
            receipt_number = pmt.receipt.receipt_number

        payments.append({
            "id":             pmt.id,
            "amount":         pmt.amount,
            "method":         pmt.method,
            "reference":      pmt.reference,
            "status":         pmt.status,
            "created_at":     pmt.created_at.strftime("%d %b %Y") if pmt.created_at else None,
            "receipt_number": receipt_number,
        })

    term = Term.query.get(invoice.term_id) if invoice.term_id else None
    year = AcademicYear.query.get(invoice.year_id) if invoice.year_id else None

    return {
        "id":            invoice.id,
        "term":          term.name if term else None,
        "term_id":       invoice.term_id,
        "academic_year": year.name if year else None,
        "year_id":       invoice.year_id,
        "total_amount":  invoice.total_amount,
        "amount_paid":   invoice.amount_paid,
        "balance":       invoice.balance,
        "created_at":    invoice.created_at.strftime("%d %b %Y") if invoice.created_at else None,
        "items":         items,
        "payments":      payments,
    }


def _serialize_medical(medical):
    if not medical:
        return None
    return {
        "has_asthma":        medical.has_asthma,
        "has_heart_problem": medical.has_heart_problem,
        "has_sickle_cell":   medical.has_sickle_cell,
        "has_hiv":           medical.has_hiv,
        "other_conditions":  medical.other_conditions,
    }


def _serialize_academic(academic):
    if not academic:
        return None
    return {
        "admission_date":    (
            academic.date_of_admission.strftime("%d %b %Y")
            if academic.date_of_admission else None
        ),
        "level":             academic.level,
        "house":             academic.house,
        "enrollment_status": academic.enrollment_status,
        "status":            academic.status,
        "academic_year":     academic.academic_year,
        "previous_school":   None,
    }


def _serialize_alumni_student(student, enrollment, class_obj, stream_obj, year_obj):
    return {
        "id":               student.id,
        "student_code":     student.student_code,
        "admission_number": student.admission_number,
        "name":             f"{student.first_name} {student.last_name}",
        "first_name":       student.first_name,
        "last_name":        student.last_name,
        "gender":           student.gender,
        "date_of_birth":    student.date_of_birth.strftime("%d %b %Y") if student.date_of_birth else None,
        "nationality":      student.nationality,
        "photo_url":        student.photo_url,
        "class_name":       class_obj.name  if class_obj  else None,
        "stream_name":      stream_obj.name if stream_obj else None,
        "academic_year":    year_obj.name   if year_obj   else None,
        "academic_year_id": year_obj.id     if year_obj   else None,
        "enrollment_status":       enrollment.status,
        "enrollment_status_label": STATUS_LABELS.get(enrollment.status, enrollment.status),
        "enrolled_on":      enrollment.created_at.strftime("%d %b %Y") if enrollment.created_at else None,
        "current_class_id": student.class_id,
    }


# ═══════════════════════════════════════════════════════════════
#  PAGE  —  GET /api/academics/alumni/
# ═══════════════════════════════════════════════════════════════

@alumni_api.route("/", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def alumni_page():
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, user_id, modules = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    classes = (
        Class.query
        .filter_by(school_id=school_id)
        .order_by(Class.name)
        .all()
    )

    academic_years = (
        AcademicYear.query
        .order_by(AcademicYear.name.desc())
        .all()
    )

    return render_template(
        "modules/academics/alumni.html",
        school=school,
        modules=modules,
        classes=classes,
        academic_years=academic_years,
    )


# ═══════════════════════════════════════════════════════════════
#  COHORT SUMMARY  —  GET /api/academics/alumni/summary
# ═══════════════════════════════════════════════════════════════

@alumni_api.route("/summary", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def cohort_summary():
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    academic_year_id = request.args.get("academic_year_id", type=int)
    class_id         = request.args.get("class_id",         type=int)

    try:
        query = (
            db.session.query(
                AcademicYear.id.label("year_id"),
                AcademicYear.name.label("year_name"),
                Class.id.label("class_id"),
                Class.name.label("class_name"),
                StudentEnrollment.status,
                db.func.count(StudentEnrollment.id).label("count"),
            )
            .join(AcademicYear, AcademicYear.id == StudentEnrollment.academic_year_id)
            .join(Class,        Class.id        == StudentEnrollment.class_id)
            .filter(StudentEnrollment.school_id == school_id)
        )

        if academic_year_id:
            query = query.filter(StudentEnrollment.academic_year_id == academic_year_id)
        if class_id:
            query = query.filter(StudentEnrollment.class_id == class_id)

        rows = (
            query
            .group_by(
                AcademicYear.id, AcademicYear.name,
                Class.id, Class.name,
                StudentEnrollment.status,
            )
            .order_by(AcademicYear.name.desc(), Class.name)
            .all()
        )

        summary = {}
        for row in rows:
            yk = str(row.year_id)
            ck = str(row.class_id)

            if yk not in summary:
                summary[yk] = {
                    "year_id":   row.year_id,
                    "year_name": row.year_name,
                    "classes":   {},
                }

            if ck not in summary[yk]["classes"]:
                summary[yk]["classes"][ck] = {
                    "class_id":   row.class_id,
                    "class_name": row.class_name,
                    "total":      0,
                    "by_status":  {},
                }

            summary[yk]["classes"][ck]["by_status"][row.status] = row.count
            summary[yk]["classes"][ck]["total"] += row.count

        result = []
        for year_data in sorted(summary.values(), key=lambda y: y["year_name"], reverse=True):
            for class_data in sorted(year_data["classes"].values(), key=lambda c: c["class_name"]):
                result.append({
                    "year_id":   year_data["year_id"],
                    "year_name": year_data["year_name"],
                    **class_data,
                })

        return jsonify({"cohorts": result}), 200

    except Exception:
        logger.exception("cohort_summary failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to load cohort summary."}), 500


# ═══════════════════════════════════════════════════════════════
#  COHORT DETAIL  —  GET /api/academics/alumni/cohort
# ═══════════════════════════════════════════════════════════════

@alumni_api.route("/cohort", methods=["GET"])
@jwt_required()
@limiter.limit(SEARCH_LIMIT)
def cohort_detail():
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    class_id         = request.args.get("class_id",         type=int)
    academic_year_id = request.args.get("academic_year_id", type=int)
    stream_id        = request.args.get("stream_id",        type=int)
    status_filter    = request.args.get("status",           default="")
    search           = request.args.get("search",           default="").strip()
    page             = request.args.get("page",    type=int, default=1)
    per_page         = min(request.args.get("per_page", type=int, default=50), 200)

    if not class_id or not academic_year_id:
        return jsonify({"message": "class_id and academic_year_id are required"}), 400

    class_obj = Class.query.filter_by(id=class_id, school_id=school_id).first()
    if not class_obj:
        return jsonify({"message": "Class not found"}), 404

    year_obj = AcademicYear.query.get(academic_year_id)
    if not year_obj:
        return jsonify({"message": "Academic year not found"}), 404

    try:
        latest_enrollment_sq = (
            db.session.query(
                StudentEnrollment.student_id,
                db.func.max(StudentEnrollment.id).label("max_id"),
            )
            .filter(
                StudentEnrollment.school_id        == school_id,
                StudentEnrollment.class_id         == class_id,
                StudentEnrollment.academic_year_id == academic_year_id,
            )
            .group_by(StudentEnrollment.student_id)
            .subquery()
        )

        query = (
            db.session.query(StudentEnrollment, Student, Stream)
            .join(
                latest_enrollment_sq,
                db.and_(
                    StudentEnrollment.student_id == latest_enrollment_sq.c.student_id,
                    StudentEnrollment.id         == latest_enrollment_sq.c.max_id,
                )
            )
            .join(Student, Student.id == StudentEnrollment.student_id)
            .outerjoin(Stream, Stream.id == StudentEnrollment.stream_id)
            .filter(
                StudentEnrollment.school_id        == school_id,
                StudentEnrollment.class_id         == class_id,
                StudentEnrollment.academic_year_id == academic_year_id,
            )
        )

        if stream_id:
            query = query.filter(StudentEnrollment.stream_id == stream_id)
        if status_filter:
            query = query.filter(StudentEnrollment.status == status_filter)
        if search:
            like = f"%{search}%"
            query = query.filter(
                db.or_(
                    Student.first_name.ilike(like),
                    Student.last_name.ilike(like),
                    Student.admission_number.ilike(like),
                    Student.student_code.ilike(like),
                )
            )

        total  = query.count()
        offset = (page - 1) * per_page
        rows   = (
            query
            .order_by(Student.first_name, Student.last_name)
            .offset(offset)
            .limit(per_page)
            .all()
        )

        streams_for_class = (
            Stream.query
            .join(StudentEnrollment, StudentEnrollment.stream_id == Stream.id)
            .filter(
                StudentEnrollment.school_id        == school_id,
                StudentEnrollment.class_id         == class_id,
                StudentEnrollment.academic_year_id == academic_year_id,
            )
            .distinct()
            .all()
        )

        data = [
            _serialize_alumni_student(student, enrollment, class_obj, stream_obj, year_obj)
            for enrollment, student, stream_obj in rows
        ]

        return jsonify({
            "students":      data,
            "total":         total,
            "page":          page,
            "per_page":      per_page,
            "pages":         (total + per_page - 1) // per_page,
            "class_name":    class_obj.name,
            "year_name":     year_obj.name,
            "streams":       [{"id": s.id, "name": s.name} for s in streams_for_class],
            "status_counts": _status_counts(school_id, class_id, academic_year_id, stream_id),
        }), 200

    except Exception:
        logger.exception("cohort_detail failed | school_id=%s class_id=%s", school_id, class_id)
        return jsonify({"message": "Failed to load cohort data."}), 500


def _status_counts(school_id, class_id, academic_year_id, stream_id=None):
    q = (
        db.session.query(
            StudentEnrollment.status,
            db.func.count(StudentEnrollment.id).label("cnt"),
        )
        .filter(
            StudentEnrollment.school_id        == school_id,
            StudentEnrollment.class_id         == class_id,
            StudentEnrollment.academic_year_id == academic_year_id,
        )
    )
    if stream_id:
        q = q.filter(StudentEnrollment.stream_id == stream_id)

    rows = q.group_by(StudentEnrollment.status).all()
    return {r.status: r.cnt for r in rows}


# ═══════════════════════════════════════════════════════════════
#  STUDENT ENROLLMENT TIMELINE
# ═══════════════════════════════════════════════════════════════

@alumni_api.route("/student/<int:student_id>/timeline", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def student_timeline(student_id):
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    student = Student.query.filter_by(id=student_id, school_id=school_id).first()
    if not student:
        return jsonify({"message": "Student not found"}), 404

    try:
        rows = (
            db.session.query(StudentEnrollment, Class, Stream, AcademicYear)
            .join(Class,        Class.id        == StudentEnrollment.class_id)
            .join(AcademicYear, AcademicYear.id == StudentEnrollment.academic_year_id)
            .outerjoin(Stream,  Stream.id       == StudentEnrollment.stream_id)
            .filter(
                StudentEnrollment.school_id  == school_id,
                StudentEnrollment.student_id == student_id,
            )
            .order_by(AcademicYear.name, StudentEnrollment.created_at)
            .all()
        )

        timeline = [
            {
                "enrollment_id":    enrollment.id,
                "academic_year":    year_obj.name,
                "academic_year_id": year_obj.id,
                "class_name":       class_obj.name,
                "class_id":         class_obj.id,
                "stream_name":      stream_obj.name if stream_obj else None,
                "status":           enrollment.status,
                "status_label":     STATUS_LABELS.get(enrollment.status, enrollment.status),
                "enrolled_on":      enrollment.created_at.strftime("%d %b %Y") if enrollment.created_at else None,
            }
            for enrollment, class_obj, stream_obj, year_obj in rows
        ]

        return jsonify({
            "student": {
                "id":               student.id,
                "name":             f"{student.first_name} {student.last_name}",
                "admission_number": student.admission_number,
                "student_code":     student.student_code,
                "photo_url":        student.photo_url,
            },
            "timeline": timeline,
        }), 200

    except Exception:
        logger.exception("student_timeline failed | student_id=%s", student_id)
        return jsonify({"message": "Failed to load student timeline."}), 500


# ═══════════════════════════════════════════════════════════════
#  STUDENT FULL PROFILE (JSON)
# ═══════════════════════════════════════════════════════════════

@alumni_api.route("/student/<int:student_id>/profile", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def alumni_student_profile_api(student_id):
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    student = Student.query.filter_by(id=student_id, school_id=school_id).first()
    if not student:
        return jsonify({"message": "Student not found"}), 404

    try:
        guardian     = Guardian.query.filter_by(student_id=student.id).first()
        medical      = MedicalRecord.query.filter_by(student_id=student.id).first()
        academic     = StudentAcademic.query.filter_by(student_id=student.id).first()
        class_obj    = Class.query.get(student.class_id) if student.class_id else None
        documents    = Document.query.filter_by(student_id=student.id).all()

        report_cards = (
            ReportCard.query
            .filter_by(school_id=school_id, student_id=student.id)
            .order_by(ReportCard.academic_year.desc(), ReportCard.generated_at.desc())
            .all()
        )

        invoices = (
            Invoice.query
            .filter_by(school_id=school_id, student_id=student.id)
            .options(
                joinedload(Invoice.items),
                joinedload(Invoice.payments).joinedload(Payment.receipt),
            )
            .order_by(Invoice.created_at.desc())
            .all()
        )

        enrollment_rows = (
            db.session.query(StudentEnrollment, Class, Stream, AcademicYear)
            .join(Class,        Class.id        == StudentEnrollment.class_id)
            .join(AcademicYear, AcademicYear.id == StudentEnrollment.academic_year_id)
            .outerjoin(Stream,  Stream.id       == StudentEnrollment.stream_id)
            .filter(
                StudentEnrollment.school_id  == school_id,
                StudentEnrollment.student_id == student.id,
            )
            .order_by(AcademicYear.name, StudentEnrollment.created_at)
            .all()
        )

        timeline = [
            {
                "enrollment_id":    enrollment.id,
                "academic_year":    year_obj.name,
                "academic_year_id": year_obj.id,
                "class_name":       class_obj_.name,
                "class_id":         class_obj_.id,
                "stream_name":      stream_obj.name if stream_obj else None,
                "status":           enrollment.status,
                "status_label":     STATUS_LABELS.get(enrollment.status, enrollment.status),
                "enrolled_on":      enrollment.created_at.strftime("%d %b %Y") if enrollment.created_at else None,
            }
            for enrollment, class_obj_, stream_obj, year_obj in enrollment_rows
        ]

        total_billed = sum(inv.total_amount for inv in invoices)
        total_paid   = sum(inv.amount_paid  for inv in invoices)

        return jsonify({
            "student": {
                "id":               student.id,
                "student_code":     student.student_code,
                "admission_number": student.admission_number,
                "first_name":       student.first_name,
                "last_name":        student.last_name,
                "name":             f"{student.first_name} {student.last_name}",
                "gender":           student.gender,
                "date_of_birth":    student.date_of_birth.strftime("%d %b %Y") if student.date_of_birth else None,
                "nationality":      student.nationality,
                "religion":         None,
                "photo_url":        student.photo_url,
                "class_name":       class_obj.name if class_obj else None,
                "class_id":         student.class_id,
                "status":           student.student_type,
            },
            "guardian": {
                "name":         guardian.name         if guardian else None,
                "relationship": guardian.relationship if guardian else None,
                "phone":        guardian.contact      if guardian else None,
                "email":        None,
                "address":      guardian.address      if guardian else None,
                "occupation":   guardian.occupation   if guardian else None,
            } if guardian else None,
            "medical":            _serialize_medical(medical),
            "academic":           _serialize_academic(academic),
            "documents":          [_serialize_document(d) for d in documents],
            "report_cards":       [_serialize_report_card(rc) for rc in report_cards],
            "financial_summary": {
                "total_billed":  total_billed,
                "total_paid":    total_paid,
                "total_balance": total_billed - total_paid,
                "invoice_count": len(invoices),
            },
            "invoices":  [_serialize_invoice(inv) for inv in invoices],
            "timeline":  timeline,
        }), 200

    except Exception:
        logger.exception("alumni_student_profile_api failed | student_id=%s", student_id)
        return jsonify({"message": "Failed to load student profile."}), 500


# ═══════════════════════════════════════════════════════════════
#  STUDENT PROFILE PAGE (HTML)
# ═══════════════════════════════════════════════════════════════

@alumni_api.route("/student/<int:student_id>", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def alumni_student_profile(student_id):
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, user_id, modules = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    student = (
        Student.query
        .filter_by(id=student_id, school_id=school_id)
        .first()
    )
    if not student:
        return render_template("errors/404.html"), 404

    try:
        guardian  = Guardian.query.filter_by(student_id=student.id).first()
        medical   = MedicalRecord.query.filter_by(student_id=student.id).first()
        academic  = StudentAcademic.query.filter_by(student_id=student.id).first()
        documents = Document.query.filter_by(student_id=student.id).all()
        class_obj = Class.query.get(student.class_id) if student.class_id else None

        enrollment_rows = (
            db.session.query(StudentEnrollment, Class, Stream, AcademicYear)
            .join(Class,        Class.id        == StudentEnrollment.class_id)
            .join(AcademicYear, AcademicYear.id == StudentEnrollment.academic_year_id)
            .outerjoin(Stream,  Stream.id       == StudentEnrollment.stream_id)
            .filter(
                StudentEnrollment.school_id  == school_id,
                StudentEnrollment.student_id == student.id,
            )
            .order_by(AcademicYear.name, StudentEnrollment.created_at)
            .all()
        )

        timeline = [
            {
                "enrollment_id":    enrollment.id,
                "academic_year":    year_obj.name,
                "academic_year_id": year_obj.id,
                "class_name":       class_obj_.name,
                "class_id":         class_obj_.id,
                "stream_name":      stream_obj.name if stream_obj else None,
                "status":           enrollment.status,
                "status_label":     STATUS_LABELS.get(enrollment.status, enrollment.status),
                "enrolled_on":      (
                    enrollment.created_at.strftime("%d %b %Y")
                    if enrollment.created_at else None
                ),
            }
            for enrollment, class_obj_, stream_obj, year_obj in enrollment_rows
        ]

        return render_template(
            "modules/academics/alumni_student_profile.html",
            student=student,
            guardian=guardian,
            medical=medical,
            documents=documents,
            academic=academic,
            class_obj=class_obj,
            school_id=school_id,
            school=school,
            modules=modules,
            timeline=timeline,
            opened_from_alumni=True,
            student_id=student_id,
        )

    except Exception:
        logger.exception("alumni_student_profile (HTML) failed | student_id=%s", student_id)
        return render_template("errors/500.html"), 500