"""
app/services/report_card_service.py
====================================
Complete report-card generation service for Nursery, Primary, O-Level
and A-Level (Uganda curriculum).

Responsibilities
----------------
1.  Determine whether a student belongs to Nursery, Primary, O-Level or
    A-Level based on their class name.
2.  Fetch all required data from the database (marks, subjects, teacher
    assignments, school info, attendance, etc.).
3.  Compute grades, aggregates and positions using Uganda curriculum
    grading standards.
4.  Hydrate the correct Jinja template — using a school-specific
    override when one is registered, otherwise the default template
    for that report type.
5.  Optionally convert the rendered HTML to PDF via WeasyPrint.
6.  Store the output file locally under /static/report_cards/.

Uganda Grading Systems
-----------------------
Nursery  — A / B / C / D / E  (qualitative)
Primary  — D1 … F9  (grade points 1–9, best 4 aggregated for division)
O-Level  — D1 … F9  (same letter scheme as primary; best 8 subjects for
            division: Div 1 = 8–32, Div 2 = 33–46, Div 3 = 47–58,
            Div 4 = 59–72, Fail = 73+)
A-Level  — A / B / C / D / E / O / F  (points: A=6 B=5 C=4 D=3 E=2 O=1
            F=0; best 3 principal subjects totalled; subsidiary counted
            separately as passes/failures)

Subsidiary subject thresholds (A-Level)
-----------------------------------------
All three subsidiary types (GP, ICT/Computer, Sub Math) use school-specific
minimum marks stored in SchoolDetail:
  - gp_min_mark       → General Paper / GP
  - ict_min_mark      → ICT / Computer Studies
  - sub_math_min_mark → Subsidiary Mathematics / Sub Math
Students scoring below the configured threshold earn 0 points (grade "F")
for that subsidiary subject.  The default for all three is 40.0%.

Grading scheme resolution
--------------------------
`fetch_grade_scales()` looks up GradeScale rows for the school + section.
If the school has configured its own scale, those rows are used for every
grade computation AND for the grading-scheme table rendered on the report
(via `_build_grade_legend()`).  Only when a school has zero GradeScale rows
do we fall back to the Uganda-standard DEFAULT_*_GRADES tables below.  This
applies uniformly — no school is special-cased for grading, only for
template selection (see _SCHOOL_TEMPLATE_OVERRIDES below).

School-specific templates
--------------------------
`_SCHOOL_TEMPLATE_OVERRIDES` lets specific schools use their own report
card design instead of the shared default template for a report_type.
`get_template_name()` resolves which template a given (school_id,
report_type) pair should render. Add new schools/report types to that
dict as needed — no other code changes required.

Performance notes
-----------------
- All per-stream data (subjects, papers, teachers, students) is loaded
  in bulk before any per-subject or per-student loop.
- `BatchContext` caches the stream-wide position map and the bulk lookups
  so that bulk report generation calls `generate()` N times with zero
  repeated stream-level queries.
- Grade-scale scanning is unified through `_apply_grade_scale()`.
- `compute_attendance()` uses a single query with conditional aggregation.
- `calculate_stream_positions()` fetches only the columns it needs (no
  full ORM object construction) and accepts pre-loaded data to avoid
  redundant DB calls.
"""

import os
import re as _re
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import render_template
from sqlalchemy import func, case, select

from app.extensions import db
from app.models.people import Student, Staff
from app.models.academic_structure import (
    Class, Stream, AcademicYear, Term,
    Subject, Papers,
    TeachAssignment, Assessment, AssessmentType,
    StudentMark, StudentStream, StudentAttendance,
    LessonSession, GradeScale,
)
from app.models.reportcards import PrimaryReportSummary

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  SMALL UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _r(v: Optional[float], n: int = 2) -> Optional[float]:
    """Round *v* to *n* decimal places, or return None if *v* is None."""
    return round(v, n) if v is not None else None


_FORMATIVE_PAT = _re.compile(r'^a\d$', _re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
#  CLASS NAME CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

NURSERY_CLASS_PREFIXES = frozenset({
    "baby", "middle", "top", "nursery", "pre", "kg", "kindergarten",
    "reception",
})

PRIMARY_CLASS_PREFIXES = frozenset({"p1", "p2", "p3", "p4", "p5", "p6", "p7", "primary"})

OLEVEL_CLASS_PREFIXES  = frozenset({"s1", "s2", "s3", "s4"})
ALEVEL_CLASS_PREFIXES  = frozenset({"s5", "s6"})


def classify_class(class_name: str) -> str:
    """
    Return 'nursery', 'primary', 'olevel', or 'alevel' based on class name.

    Matching order (first hit wins):
      A-Level  → class starts with s5 / s6
      O-Level  → class starts with s1–s4
      Nursery  → nursery prefixes
      Primary  → primary prefixes
      Default  → 'primary'
    """
    if not class_name:
        logger.warning("classify_class called with empty class_name; defaulting to primary")
        return "primary"

    normalised = class_name.strip().lower()

    for prefix in ALEVEL_CLASS_PREFIXES:
        if normalised == prefix or normalised.startswith(prefix):
            return "alevel"

    for prefix in OLEVEL_CLASS_PREFIXES:
        if normalised == prefix or normalised.startswith(prefix):
            return "olevel"

    for prefix in NURSERY_CLASS_PREFIXES:
        if normalised == prefix or normalised.startswith(prefix):
            return "nursery"

    for prefix in PRIMARY_CLASS_PREFIXES:
        if normalised == prefix or normalised.startswith(prefix):
            return "primary"

    logger.warning(
        "classify_class: unrecognised class name '%s'; defaulting to primary", class_name
    )
    return "primary"


# ─────────────────────────────────────────────────────────────────────────────
#  GRADING CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Division boundary constants — named for clarity
_PRIMARY_DIV1_MAX  = 12
_PRIMARY_DIV2_MAX  = 23
_PRIMARY_DIV3_MAX  = 29
_PRIMARY_DIV4_MAX  = 34

_OLEVEL_DIV1_MAX   = 32
_OLEVEL_DIV2_MAX   = 46
_OLEVEL_DIV3_MAX   = 58
_OLEVEL_DIV4_MAX   = 72

DEFAULT_PRIMARY_GRADES = [
    (80, 100, "D1", "Excellent"),
    (75,  79, "D2", "Very Good"),
    (65,  74, "C3", "Good"),
    (60,  64, "C4", "Good"),
    (55,  59, "C5", "Fairly Good"),
    (50,  54, "C6", "Fair"),
    (40,  49, "P7", "Pass"),
    (30,  39, "P8", "Weak Pass"),
    ( 0,  29, "F9", "Fail"),
]

PRIMARY_GRADE_POINTS: dict[str, int] = {
    "D1": 1, "D2": 2, "C3": 3, "C4": 4,
    "C5": 5, "C6": 6, "P7": 7, "P8": 8, "F9": 9,
}

# O-Level uses the same letter/point scheme as Primary
DEFAULT_OLEVEL_GRADES  = DEFAULT_PRIMARY_GRADES
OLEVEL_GRADE_POINTS    = PRIMARY_GRADE_POINTS

DEFAULT_ALEVEL_GRADES = [
    (80, 100, "A", "Excellent"),
    (70,  79, "B", "Very Good"),
    (60,  69, "C", "Good"),
    (55,  59, "D", "Satisfactory"),
    (50,  54, "E", "Fair"),
    (40,  49, "O", "Ordinary Pass"),
    ( 0,  39, "F", "Fail"),
]

ALEVEL_GRADE_POINTS: dict[str, int] = {
    "A": 6, "B": 5, "C": 4, "D": 3, "E": 2, "O": 1, "F": 0,
}

ALEVEL_GRADE_COMMENTS: dict[str, str] = {
    "A": "Excellent performance",
    "B": "Very good performance",
    "C": "Good performance",
    "D": "Satisfactory performance",
    "E": "Fair performance",
    "O": "Ordinary pass",
    "F": "Fail — improvement needed",
}

DEFAULT_NURSERY_GRADES = [
    (90, 100, "A", "Excellent"),
    (80,  89, "B", "Very Good"),
    (70,  79, "C", "Good"),
    (60,  69, "D", "Fair"),
    ( 0,  59, "E", "Needs Improvement"),
]

NURSERY_QUALITATIVE_MAP: dict[str, str] = {
    "A": "Excellent", "B": "Very Good", "C": "Good",
    "D": "Fair",      "E": "Needs Improvement",
}

# ─────────────────────────────────────────────────────────────────────────────
#  SUBSIDIARY SUBJECT DETECTION  (A-Level)
# ─────────────────────────────────────────────────────────────────────────────

# General Paper / GP prefixes
_GP_PREFIXES: tuple[str, ...] = (
    "general paper",
    "gp",
)

# ICT / Computer prefixes
_ICT_PREFIXES: tuple[str, ...] = (
    "ict",
    "information communication technology",
    "sub ict",
    "subsidiary ict",
    "computer studies",
    "computer science",
    "computer",
)

# Subsidiary Mathematics prefixes
_SUB_MATH_PREFIXES: tuple[str, ...] = (
    "subsidiary mathematics",
    "subsidiary maths",
    "subsidiary math",
    "sub mathematics",
    "sub maths",
    "sub math",
)

# Combined set used for quick "is this subsidiary?" checks
SUBSIDIARY_SUBJECT_PREFIXES: tuple[str, ...] = (
    *_GP_PREFIXES,
    *_ICT_PREFIXES,
    *_SUB_MATH_PREFIXES,
)

_DEFAULT_SUBSIDIARY_PASS_PCT: float = 40.0


def _subsidiary_type(subject_name: str) -> str:
    """
    Return the subsidiary category for a subject name:
      'ict'      — ICT / Computer Studies
      'sub_math' — Subsidiary Mathematics
      'gp'       — General Paper (and anything else subsidiary)
      ''         — not a subsidiary subject
    """
    name_lower = (subject_name or "").strip().lower()

    for prefix in _ICT_PREFIXES:
        if name_lower.startswith(prefix):
            return "ict"

    for prefix in _SUB_MATH_PREFIXES:
        if name_lower.startswith(prefix):
            return "sub_math"

    for prefix in _GP_PREFIXES:
        if name_lower.startswith(prefix):
            return "gp"

    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  UNIFIED GRADE SCALE APPLICATOR  (eliminates 4 copies of the same loop)
# ─────────────────────────────────────────────────────────────────────────────

def _apply_grade_scale(
    score: float,
    grade_scales: list,
    default_table: list[tuple[int, int, str, str]],
    no_match_grade: str,
    no_match_remark: str,
) -> tuple[str, str]:
    """
    Return (grade, remark) by scanning *grade_scales* first, then
    *default_table*, then returning (*no_match_grade*, *no_match_remark*).

    *grade_scales* is whatever the calling school has configured in the
    GradeScale table for this section. If the school has rows there, those
    rows are authoritative and the Uganda-standard *default_table* is never
    consulted. Only schools with zero configured rows fall back to defaults.
    """
    if grade_scales:
        for gs in grade_scales:
            if gs.min_score <= score <= gs.max_score:
                return gs.grade, (gs.remark or "")
        return no_match_grade, no_match_remark
    for lo, hi, grade, remark in default_table:
        if lo <= score <= hi:
            return grade, remark
    return no_match_grade, no_match_remark


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC GRADE FUNCTIONS  (thin wrappers)
# ─────────────────────────────────────────────────────────────────────────────

def primary_grade(score: float, grade_scales: list) -> tuple[str, str]:
    return _apply_grade_scale(score, grade_scales, DEFAULT_PRIMARY_GRADES, "F9", "Fail")


def olevel_grade(score: float, grade_scales: list) -> tuple[str, str]:
    return _apply_grade_scale(score, grade_scales, DEFAULT_OLEVEL_GRADES, "F9", "Fail")


def alevel_grade(score: float, grade_scales: list) -> tuple[str, str]:
    return _apply_grade_scale(score, grade_scales, DEFAULT_ALEVEL_GRADES, "F", "Fail")


def nursery_grade(score: float, grade_scales: list) -> tuple[str, str]:
    grade, remark = _apply_grade_scale(
        score, grade_scales, DEFAULT_NURSERY_GRADES, "E", "Needs Improvement"
    )
    if not remark:
        remark = NURSERY_QUALITATIVE_MAP.get(grade, "")
    return grade, remark


# ─────────────────────────────────────────────────────────────────────────────
#  SUBSIDIARY SUBJECT HELPERS  (A-Level)
# ─────────────────────────────────────────────────────────────────────────────

def is_subsidiary_subject(subject_name: str) -> bool:
    """Return True if the subject name matches a known subsidiary pattern."""
    return _subsidiary_type(subject_name) != ""


def _get_subsidiary_threshold(subject_name: str, school_detail) -> float:
    """
    Return the pass threshold (percentage) for a subsidiary subject,
    drawn from the school's configured minimums in SchoolDetail:
      - ict_min_mark      for ICT / Computer Studies
      - sub_math_min_mark for Subsidiary Mathematics
      - gp_min_mark       for General Paper (and all other subsidiaries)

    Falls back to _DEFAULT_SUBSIDIARY_PASS_PCT (40 %) when school_detail
    is None or the relevant attribute is unset / zero.
    """
    stype = _subsidiary_type(subject_name)

    if school_detail is None:
        return _DEFAULT_SUBSIDIARY_PASS_PCT

    if stype == "ict":
        return float(getattr(school_detail, "ict_min_mark", None) or _DEFAULT_SUBSIDIARY_PASS_PCT)

    if stype == "sub_math":
        return float(getattr(school_detail, "sub_math_min_mark", None) or _DEFAULT_SUBSIDIARY_PASS_PCT)

    # "gp" and any unrecognised subsidiary fall through to GP threshold
    return float(getattr(school_detail, "gp_min_mark", None) or _DEFAULT_SUBSIDIARY_PASS_PCT)


def alevel_subsidiary_grade(
    score: float,
    subject_name: str,
    school_detail,
) -> tuple[str, str, int]:
    """
    Return (grade_label, remark, points) for a subsidiary subject.

    Grade is "P" (pass) or "F" (fail); points are 1 or 0 respectively.

    The pass threshold is read from SchoolDetail:
      - gp_min_mark       for General Paper
      - ict_min_mark      for ICT / Computer Studies
      - sub_math_min_mark for Subsidiary Mathematics
    """
    threshold = _get_subsidiary_threshold(subject_name, school_detail)

    if score >= threshold:
        return "P", f"Subsidiary Pass (≥{threshold:.0f}%)", 1
    return "F", f"Subsidiary Fail (<{threshold:.0f}%)", 0


# ─────────────────────────────────────────────────────────────────────────────
#  AGGREGATE / DIVISION / POINTS CALCULATIONS
# ─────────────────────────────────────────────────────────────────────────────

def compute_primary_division(aggregates: int) -> str:
    if aggregates <= _PRIMARY_DIV1_MAX:
        return "Division 1"
    elif aggregates <= _PRIMARY_DIV2_MAX:
        return "Division 2"
    elif aggregates <= _PRIMARY_DIV3_MAX:
        return "Division 3"
    elif aggregates <= _PRIMARY_DIV4_MAX:
        return "Division 4"
    return "Ungraded (U)"


def compute_primary_aggregates(
    subject_rows: list,
    grade_scales: list,
) -> tuple[Optional[int], Optional[str]]:
    if not subject_rows:
        return None, None
    grade_point_list = [
        PRIMARY_GRADE_POINTS.get(row.get("grade", "F9"), 9)
        for row in subject_rows
    ]
    if not grade_point_list:
        return None, None
    aggregates = sum(sorted(grade_point_list)[:4])
    return aggregates, compute_primary_division(aggregates)


def compute_olevel_aggregates(
    subject_rows: list,
    grade_scales: list,
) -> tuple[Optional[int], Optional[str]]:
    """Compute O-Level aggregates and division (best 8 subjects)."""
    if not subject_rows:
        return None, None
    grade_points = [
        OLEVEL_GRADE_POINTS.get(row.get("grade", "F9"), 9)
        for row in subject_rows
        if row.get("grade") and row.get("grade") != "—"
    ]
    if not grade_points:
        return None, None
    aggregates = sum(sorted(grade_points)[:8])
    if aggregates <= _OLEVEL_DIV1_MAX:
        division = "Division 1"
    elif aggregates <= _OLEVEL_DIV2_MAX:
        division = "Division 2"
    elif aggregates <= _OLEVEL_DIV3_MAX:
        division = "Division 3"
    elif aggregates <= _OLEVEL_DIV4_MAX:
        division = "Division 4"
    else:
        division = "Fail (U)"
    return aggregates, division


def compute_alevel_points(
    subject_rows: list,
    grade_scales: list,
) -> tuple[Optional[int], Optional[int]]:
    """
    Return (total_points, subsidiary_points).

    total_points = best 3 principal subject points + all subsidiary points.
    Subsidiary points are 1 (pass) or 0 (fail) as set by alevel_subsidiary_grade().
    """
    if not subject_rows:
        return None, None

    principal_pts: list[int] = []
    subsidiary_pts_total = 0

    for row in subject_rows:
        if row.get("is_summary"):
            continue
        if row.get("is_subsidiary"):
            subsidiary_pts_total += int(row.get("alevel_points") or 0)
        else:
            grade = (row.get("grade") or "").strip().upper()
            if not grade or grade in ("—", "F"):
                continue
            principal_pts.append(ALEVEL_GRADE_POINTS.get(grade, 0))

    if not principal_pts and subsidiary_pts_total == 0:
        return None, None

    total_points = sum(sorted(principal_pts, reverse=True)[:3]) + subsidiary_pts_total
    return total_points, subsidiary_pts_total


# ─────────────────────────────────────────────────────────────────────────────
#  GRADE SCALE FETCHER
# ─────────────────────────────────────────────────────────────────────────────

def fetch_grade_scales(school_id: int, section_category: Optional[str] = None) -> list:
    """
    Return the GradeScale rows a school has configured for a given section
    ('Nursery' / 'Primary' / 'O Level' / 'A Level'), ordered highest-first.

    An empty list here means "this school hasn't configured a custom
    scheme" — every grade_fn / _build_grade_legend() call interprets an
    empty list as "use the Uganda-standard defaults".
    """
    q = GradeScale.query.filter_by(school_id=school_id)
    if section_category:
        q = q.filter_by(section_category=section_category)
    return q.order_by(GradeScale.min_score.desc()).all()


# ─────────────────────────────────────────────────────────────────────────────
#  BULK DATA LOADERS  (load once, reuse many times)
# ─────────────────────────────────────────────────────────────────────────────

def _load_subjects_map(subject_ids: list[int]) -> dict[int, Subject]:
    """Return {subject_id: Subject} for all given ids in one query."""
    if not subject_ids:
        return {}
    rows = Subject.query.filter(Subject.id.in_(subject_ids)).all()
    return {s.id: s for s in rows}


def _load_papers_map(subject_ids: list[int], school_id: int) -> dict[int, list]:
    """Return {subject_id: [Paper, ...]} for all given subject ids in one query."""
    if not subject_ids:
        return {}
    rows = (
        Papers.query
        .filter(Papers.subject_id.in_(subject_ids), Papers.school_id == school_id)
        .order_by(Papers.subject_id, Papers.paper_name)
        .all()
    )
    result: dict[int, list] = {}
    for p in rows:
        result.setdefault(p.subject_id, []).append(p)
    return result


def _load_teacher_map(
    school_id: int, stream_id: int, subject_ids: list[int]
) -> dict[int, str]:
    """
    Return {subject_id: "First Last"} for all subjects in one query.

    Only resolves teachers assigned to exactly one staff member for the
    given stream. Subjects with 0 or 2+ assignments map to an empty string.
    """
    if not subject_ids:
        return {}

    rows = (
        db.session.query(
            TeachAssignment.subject_id,
            Staff.first_name,
            Staff.last_name,
        )
        .join(Staff, Staff.id == TeachAssignment.staff_id)
        .filter(
            TeachAssignment.school_id == school_id,
            TeachAssignment.stream_id == stream_id,
            TeachAssignment.subject_id.in_(subject_ids),
        )
        .all()
    )

    counts: dict[int, int] = {}
    data: dict[int, str] = {}
    for subj_id, first, last in rows:
        counts[subj_id] = counts.get(subj_id, 0) + 1
        data[subj_id] = f"{first or ''} {last or ''}".strip()

    return {subj_id: (name if counts.get(subj_id) == 1 else "") for subj_id, name in data.items()}


def _initials_from_name(name: str) -> str:
    """Return teacher initials, e.g. 'B.S', from a full name."""
    if not name:
        return ""
    return ".".join(p[0].upper() for p in name.split() if p)


# ─────────────────────────────────────────────────────────────────────────────
#  MARKS DATA FETCHER
# ─────────────────────────────────────────────────────────────────────────────

def fetch_student_marks(
    school_id: int,
    student_id: int,
    term_id: int,
    exam_enum,
    stream_id: Optional[int] = None,
) -> dict:
    """
    Fetch all marks for one student in one term/exam.

    Returns { subject_id: { paper_id_or_None: score_float, ... }, ... }
    """
    if stream_id is None:
        ss_row = StudentStream.query.filter_by(
            student_id=student_id, school_id=school_id,
        ).first()
        if not ss_row:
            return {}
        stream_id = ss_row.stream_id

    assessments = Assessment.query.filter(
        Assessment.school_id == school_id,
        Assessment.stream_id == stream_id,
        Assessment.term_id   == term_id,
        Assessment.type      == exam_enum,
    ).all()

    if not assessments:
        return {}

    assessment_ids = [a.id for a in assessments]

    mark_rows = db.session.execute(
        select(StudentMark.assessment_id, StudentMark.score)
        .where(
            StudentMark.assessment_id.in_(assessment_ids),
            StudentMark.student_id == student_id,
        )
    ).fetchall()

    if not mark_rows:
        return {}

    asmt_map = {a.id: (a.subject_id, a.paper_id) for a in assessments}

    result: dict = {}
    for assessment_id, score in mark_rows:
        key = asmt_map.get(assessment_id)
        if not key:
            continue
        subj_id, paper_id = key
        if subj_id is None:
            continue
        result.setdefault(subj_id, {})[paper_id] = score

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  TEACHER NAME RESOLVERS  (kept for backward compatibility)
# ─────────────────────────────────────────────────────────────────────────────

def resolve_subject_teacher(school_id: int, stream_id: int, subject_id: int) -> str:
    """Single-subject teacher resolution. Use _load_teacher_map() for bulk use."""
    teacher_map = _load_teacher_map(school_id, stream_id, [subject_id])
    return teacher_map.get(subject_id, "")


def resolve_subject_teacher_initials(school_id: int, stream_id: int, subject_id: int) -> str:
    """Return teacher initials. Derives from resolve_subject_teacher() — no extra DB call."""
    return _initials_from_name(resolve_subject_teacher(school_id, stream_id, subject_id))


# ─────────────────────────────────────────────────────────────────────────────
#  ATTENDANCE SUMMARY  (single query with conditional aggregation)
# ─────────────────────────────────────────────────────────────────────────────

def compute_attendance(school_id: int, student_id: int, term_id: int) -> dict:
    """
    Return {"total": int, "present": int, "absent": int} for one student
    in one term.

    NOTE: This join uses LessonSession.term_id. If your LessonSession model
    uses a different column name (e.g. semester_id, period_id), update the
    .where() clause below to match. Run this to check:
        from app.models.academic_structure import LessonSession
        print([c.name for c in LessonSession.__table__.columns])
    """
    try:
        row = db.session.execute(
            select(
                func.count(StudentAttendance.id).label("total"),
                func.sum(
                    case((StudentAttendance.status == "present", 1), else_=0)
                ).label("present"),
            )
            .join(LessonSession, LessonSession.id == StudentAttendance.lesson_id)
            .where(
                StudentAttendance.school_id  == school_id,
                StudentAttendance.student_id == student_id,
                LessonSession.term_id        == term_id,  # ← update column name if needed
            )
        ).one()

        total   = int(row.total   or 0)
        present = int(row.present or 0)
        return {"total": total, "present": present, "absent": total - present}

    except Exception as exc:
        logger.error(
            "compute_attendance failed for student=%s term=%s: %s",
            student_id, term_id, exc,
        )
        return {"total": 0, "present": 0, "absent": 0}


# ─────────────────────────────────────────────────────────────────────────────
#  PRIMARY / NURSERY SUBJECT ROWS BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_subject_rows(
    school_id:    int,
    stream_id:    int,
    student_id:   int,
    marks_data:   dict,
    grade_scales: list,
    report_type:  str,
    subjects_map: Optional[dict] = None,
    papers_map:   Optional[dict] = None,
    teacher_map:  Optional[dict] = None,
) -> list:
    """
    Build subject row dicts for primary / nursery templates.

    *subjects_map*, *papers_map*, and *teacher_map* are optional pre-loaded
    dicts. When omitted the function falls back to individual DB queries
    (single-student convenience usage). Pass them in from BatchContext
    for bulk generation.
    """
    subject_ids = list(marks_data.keys())

    if subjects_map is None:
        subjects_map = _load_subjects_map(subject_ids)
    if papers_map is None:
        papers_map = _load_papers_map(subject_ids, school_id)
    if teacher_map is None:
        teacher_map = _load_teacher_map(school_id, stream_id, subject_ids)

    grade_fn = nursery_grade if report_type == "nursery" else primary_grade

    rows = []
    for subj_id, paper_scores in marks_data.items():
        subject = subjects_map.get(subj_id)
        if not subject:
            continue

        teacher_name = teacher_map.get(subj_id, "")
        papers       = papers_map.get(subj_id, [])

        if papers:
            paper_rows  = []
            total_score = 0.0
            total_max   = 0.0

            for paper in papers:
                score = paper_scores.get(paper.id)
                max_m = float(paper.max_marks) if paper.max_marks else 100.0

                if score is not None:
                    pct         = (score / max_m) * 100.0 if max_m else 0.0
                    g, r        = grade_fn(pct, grade_scales)
                    total_score += score
                    total_max   += max_m
                    paper_rows.append({
                        "name": paper.paper_name or "Paper",
                        "score": round(score, 1),
                        "max": int(max_m),
                        "grade": g,
                        "remark": r,
                    })
                else:
                    paper_rows.append({
                        "name": paper.paper_name or "Paper",
                        "score": None,
                        "max": int(max_m),
                        "grade": "—",
                        "remark": "—",
                    })

            if total_max > 0:
                subject_pct  = (total_score / total_max) * 100.0
                subj_grade, subj_remark = grade_fn(subject_pct, grade_scales)
                subj_score   = round(subject_pct, 1)
            else:
                subj_grade = subj_remark = "—"
                subj_score = None

            rows.append({
                "subject_name": subject.name,
                "teacher_name": teacher_name,
                "total_score":  subj_score,
                "grade":        subj_grade,
                "remark":       subj_remark,
                "papers":       paper_rows,
                "has_papers":   True,
            })

        else:
            score = paper_scores.get(None)
            if score is not None:
                g, r = grade_fn(float(score), grade_scales)
            else:
                g, r = "—", "—"

            rows.append({
                "subject_name": subject.name,
                "teacher_name": teacher_name,
                "total_score":  round(float(score), 1) if score is not None else None,
                "grade":        g,
                "remark":       r,
                "papers":       [],
                "has_papers":   False,
            })

    rows.sort(key=lambda x: x["subject_name"])
    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  SECONDARY (O-LEVEL / A-LEVEL) SUBJECT ROWS BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_secondary_subject_rows(
    school_id:    int,
    stream_id:    int,
    student_id:   int,
    marks_data:   dict,
    grade_scales: list,
    report_type:  str,
    school_detail=None,
    subjects_map: Optional[dict] = None,
    papers_map:   Optional[dict] = None,
    teacher_map:  Optional[dict] = None,
) -> list:
    """
    Build subject row dicts for O-Level / A-Level templates.

    For A-Level subsidiary subjects (GP, ICT, Sub Math) the pass threshold
    is read from SchoolDetail:
      - gp_min_mark       → General Paper
      - ict_min_mark      → ICT / Computer Studies
      - sub_math_min_mark → Subsidiary Mathematics
    A student scoring below the threshold earns 0 points (grade "F").
    """
    subject_ids = list(marks_data.keys())

    if subjects_map is None:
        subjects_map = _load_subjects_map(subject_ids)
    if papers_map is None:
        papers_map = _load_papers_map(subject_ids, school_id)
    if teacher_map is None:
        teacher_map = _load_teacher_map(school_id, stream_id, subject_ids)

    grade_fn = alevel_grade if report_type == "alevel" else olevel_grade
    rows: list = []

    for subj_id, paper_scores in marks_data.items():
        subject = subjects_map.get(subj_id)
        if not subject:
            continue

        teacher_name     = teacher_map.get(subj_id, "")
        teacher_initials = _initials_from_name(teacher_name)

        # Subsidiary detection — only meaningful for A-Level
        subsidiary = (report_type == "alevel") and is_subsidiary_subject(subject.name)

        all_papers_for_subj = papers_map.get(subj_id, [])

        formative_papers = [p for p in all_papers_for_subj if _FORMATIVE_PAT.match(p.paper_name or "")]
        eot_papers       = [p for p in all_papers_for_subj if not _FORMATIVE_PAT.match(p.paper_name or "")]

        # ── No papers in DB → single-score fallback ───────────────────────────
        if not all_papers_for_subj:
            raw       = paper_scores.get(None)
            score_pct = _r(float(raw), 1) if raw is not None else None

            if subsidiary and score_pct is not None:
                grade, comment, pts = alevel_subsidiary_grade(
                    score_pct, subject.name, school_detail
                )
            elif score_pct is not None:
                grade, comment = grade_fn(score_pct, grade_scales)
                pts = ALEVEL_GRADE_POINTS.get(grade, 0) if report_type == "alevel" else None
            else:
                grade, comment, pts = "—", "—", None

            rows.append({
                "subject_name":     subject.name,
                "subject_code":     getattr(subject, "code", "") or "",
                "teacher_name":     teacher_name,
                "teacher_initials": teacher_initials,
                "num_papers":       1,
                "is_subsidiary":    subsidiary,
                "alevel_points":    pts,
                "a1": None, "a2": None, "a3": None,
                "cbc_avg":          None,
                "formative_max":    100.0,
                "formative_20":     None,
                "exam_80":          None,
                "total_100":        score_pct,
                "grade":            grade,
                "comment":          comment,
                "papers": [{"paper_num": 1, "exam_score": raw, "exam_max": 100.0, "is_first": True}],
                "is_summary":       False,
                "row_index":        0,
            })
            continue

        # ── Formative: A1, A2, A3 ────────────────────────────────────────────
        f_scores: list = []
        formative_max: float = 100.0

        for fp in formative_papers[:3]:
            sc = paper_scores.get(fp.id)
            f_scores.append(sc)
            if formative_max == 100.0 and fp.max_marks:
                formative_max = float(fp.max_marks)

        while len(f_scores) < 3:
            f_scores.append(None)

        a1, a2, a3 = f_scores[0], f_scores[1], f_scores[2]
        valid_f    = [s for s in [a1, a2, a3] if s is not None]
        cbc_avg    = _r(sum(valid_f) / len(valid_f), 2) if valid_f else None

        formative_20 = (
            _r((cbc_avg / formative_max) * 20.0, 2)
            if cbc_avg is not None else None
        )

        # ── Summative: EOT scores per paper ──────────────────────────────────
        if not eot_papers:
            raw = paper_scores.get(None)
            eot_entry         = [{"paper_num": 1, "exam_score": _r(float(raw), 1) if raw is not None else None, "exam_max": 100.0, "is_first": True}]
            sum_scores        = float(raw) if raw is not None else 0.0
            papers_with_marks = 1 if raw is not None else 0
            total_eot_papers  = 1
        else:
            eot_entry         = []
            sum_scores        = 0.0
            papers_with_marks = 0
            total_eot_papers  = len(eot_papers)

            for idx, ep in enumerate(eot_papers):
                sc = paper_scores.get(ep.id)
                mx = float(ep.max_marks) if ep.max_marks else 100.0
                if sc is not None:
                    sum_scores        += float(sc)
                    papers_with_marks += 1
                eot_entry.append({
                    "paper_num":  idx + 1,
                    "exam_score": _r(float(sc), 1) if sc is not None else None,
                    "exam_max":   mx,
                    "is_first":   idx == 0,
                })

        if eot_entry:
            eot_entry[0]["is_first"] = True

        # ── 80% formula ───────────────────────────────────────────────────────
        exam_80 = (
            round((sum_scores / (100.0 * total_eot_papers)) * 80.0, 1)
            if papers_with_marks > 0 else None
        )

        # ── 100% = 20% + 80% ─────────────────────────────────────────────────
        total_100 = (
            _r((formative_20 or 0.0) + (exam_80 or 0.0), 2)
            if (formative_20 is not None or exam_80 is not None) else None
        )

        # ── Grade ─────────────────────────────────────────────────────────────
        if subsidiary and total_100 is not None:
            grade, comment, pts = alevel_subsidiary_grade(
                total_100, subject.name, school_detail
            )
        elif total_100 is not None:
            grade, comment = grade_fn(total_100, grade_scales)
            pts = ALEVEL_GRADE_POINTS.get(grade, 0) if report_type == "alevel" else None
        else:
            grade, comment, pts = "—", "—", None

        rows.append({
            "subject_name":     subject.name,
            "subject_code":     getattr(subject, "code", "") or "",
            "teacher_name":     teacher_name,
            "teacher_initials": teacher_initials,
            "num_papers":       len(eot_entry),
            "is_subsidiary":    subsidiary,
            "alevel_points":    pts,
            "a1":               _r(a1, 2),
            "a2":               _r(a2, 2),
            "a3":               _r(a3, 2),
            "cbc_avg":          cbc_avg,
            "formative_max":    formative_max,
            "formative_20":     formative_20,
            "exam_80":          exam_80,
            "total_100":        total_100,
            "grade":            grade,
            "comment":          comment,
            "papers":           eot_entry,
            "is_summary":       False,
            "row_index":        0,
        })

    rows.sort(key=lambda x: x["subject_name"])
    for i, row in enumerate(rows):
        row["row_index"] = i

    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  STREAM POSITIONS
# ─────────────────────────────────────────────────────────────────────────────

def calculate_stream_positions(
    school_id:   int,
    stream_id:   int,
    term_id:     int,
    exam_enum,
    grade_scales: Optional[list] = None,
) -> dict[int, int]:
    """
    Calculate class-rank positions for all students in a stream.

    Returns {student_id: position}.
    """
    ss_rows     = StudentStream.query.filter_by(school_id=school_id, stream_id=stream_id).all()
    student_ids = [ss.student_id for ss in ss_rows]

    if not student_ids:
        return {}

    stream_obj  = Stream.query.get(stream_id)
    class_name  = stream_obj.class_.name if (stream_obj and stream_obj.class_) else ""
    report_type = classify_class(class_name)

    grade_fn_map = {
        "olevel":  olevel_grade,
        "alevel":  alevel_grade,
        "nursery": nursery_grade,
        "primary": primary_grade,
    }
    grade_fn = grade_fn_map.get(report_type, primary_grade)

    assessments = Assessment.query.filter(
        Assessment.school_id == school_id,
        Assessment.stream_id == stream_id,
        Assessment.term_id   == term_id,
        Assessment.type      == exam_enum,
    ).all()

    if not assessments:
        return {sid: len(student_ids) for sid in student_ids}

    assessment_ids      = [a.id for a in assessments]
    asmt_map            = {a.id: a for a in assessments}
    subject_ids_in_play = list({a.subject_id for a in assessments if a.subject_id})

    if grade_scales is None:
        grade_scales = (
            GradeScale.query
            .filter_by(school_id=school_id)
            .order_by(GradeScale.min_score.desc())
            .all()
        )

    mark_rows = db.session.execute(
        select(
            StudentMark.assessment_id,
            StudentMark.student_id,
            StudentMark.score,
        ).where(
            StudentMark.assessment_id.in_(assessment_ids),
            StudentMark.student_id.in_(student_ids),
        )
    ).fetchall()

    all_papers = (
        Papers.query
        .filter(Papers.subject_id.in_(subject_ids_in_play), Papers.school_id == school_id)
        .all()
    )
    paper_max_map: dict[int, float] = {p.id: float(p.max_marks or 100) for p in all_papers}
    subject_papers: dict[int, list[int]] = {}
    for p in all_papers:
        subject_papers.setdefault(p.subject_id, []).append(p.id)

    student_name_map: dict[int, str] = {}
    for s in Student.query.filter(Student.id.in_(student_ids)).all():
        student_name_map[s.id] = (
            f"{(s.first_name or '').strip()} {(s.last_name or '').strip()}".strip().lower()
        )

    student_marks: dict[int, dict[int, dict]] = {}
    for assessment_id, student_id, score in mark_rows:
        asmt = asmt_map.get(assessment_id)
        if not asmt or not asmt.subject_id:
            continue
        (
            student_marks
            .setdefault(student_id, {})
            .setdefault(asmt.subject_id, {})[asmt.paper_id]
        ) = score

    student_tuples: list = []

    for sid in student_ids:
        subj_data = student_marks.get(sid, {})
        subject_rows_local: list = []
        raw_totals: list[float]  = []

        for subj_id, paper_scores in subj_data.items():
            papers_for_subj = subject_papers.get(subj_id)

            if papers_for_subj:
                total_score = 0.0
                total_max   = 0.0
                for pid in papers_for_subj:
                    sc = paper_scores.get(pid)
                    if sc is not None:
                        mx = paper_max_map.get(pid, 100.0)
                        total_score += sc
                        total_max   += mx
                        raw_totals.append(sc)
                if total_max > 0:
                    pct   = (total_score / total_max) * 100.0
                    grade, _ = grade_fn(pct, grade_scales)
                    subject_rows_local.append({"total_score": round(pct, 1), "grade": grade})
            else:
                sc = paper_scores.get(None)
                if sc is not None:
                    grade, _ = grade_fn(float(sc), grade_scales)
                    subject_rows_local.append({"total_score": round(float(sc), 1), "grade": grade})
                    raw_totals.append(sc)

        if report_type == "primary" and subject_rows_local:
            agg, _ = compute_primary_aggregates(subject_rows_local, grade_scales)
        elif report_type == "olevel" and subject_rows_local:
            agg, _ = compute_olevel_aggregates(subject_rows_local, grade_scales)
        elif report_type == "alevel" and subject_rows_local:
            pts, _ = compute_alevel_points(subject_rows_local, grade_scales)
            agg = -pts if pts is not None else None
        else:
            agg = None

        scores    = [r["total_score"] for r in subject_rows_local if r["total_score"] is not None]
        avg       = round(sum(scores) / len(scores), 6) if scores else None
        total_raw = round(sum(raw_totals), 6) if raw_totals else None
        name      = student_name_map.get(sid, "")

        student_tuples.append((sid, agg, avg, total_raw, name))

    def sort_key(tup: tuple) -> tuple:
        _sid, agg, avg, total, name = tup
        return (
            agg   is None,  agg   if agg   is not None else 0,
            avg   is None, -avg   if avg   is not None else 0,
            total is None, -total if total is not None else 0,
            name,
        )

    student_tuples.sort(key=sort_key)

    positions: dict[int, int] = {}
    prev_sort_key = None
    current_rank  = 1

    for i, tup in enumerate(student_tuples):
        sid = tup[0]
        key = sort_key(tup)
        if key != prev_sort_key:
            current_rank = i + 1
        positions[sid] = current_rank
        prev_sort_key  = key

    last_pos = len(student_tuples) + 1
    for sid in student_ids:
        positions.setdefault(sid, last_pos)

    return positions


# ─────────────────────────────────────────────────────────────────────────────
#  GRADE LEGEND BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_grade_legend(grade_scales: list, report_type: str) -> list:
    """
    Build the rows for the "GRADING SCHEME" table shown on a report card.

    If the owning school has configured GradeScale rows for this section,
    those rows are used verbatim (sorted highest-first). Only schools with
    no configured rows fall back to the Uganda-standard defaults table.
    Templates should always render from this list rather than hardcoding
    a grading table, so a school's custom scheme (if any) is reflected
    automatically.
    """
    if grade_scales:
        return [
            {
                "grade":  gs.grade,
                "min":    int(gs.min_score),
                "max":    int(gs.max_score),
                "remark": gs.remark or "",
            }
            for gs in sorted(grade_scales, key=lambda x: x.min_score, reverse=True)
        ]

    defaults_map = {
        "nursery": DEFAULT_NURSERY_GRADES,
        "primary": DEFAULT_PRIMARY_GRADES,
        "olevel":  DEFAULT_OLEVEL_GRADES,
        "alevel":  DEFAULT_ALEVEL_GRADES,
    }
    return [
        {"grade": g, "min": lo, "max": hi, "remark": r}
        for lo, hi, g, r in defaults_map.get(report_type, DEFAULT_PRIMARY_GRADES)
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  BATCH CONTEXT  (eliminates repeated stream-level queries in bulk generation)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BatchContext:
    """
    Pre-computed, stream-scoped data shared across multiple `generate()` calls.

    Usage
    -----
    Build once per stream before the per-student loop:

        ctx = BatchContext.build(school_id, stream_id, term_id, exam_enum,
                                 report_type, grade_scales)
        for student in students:
            service.generate(student, stream, term, year, exam_type,
                             static_folder, batch_ctx=ctx)
    """
    school_id:    int
    stream_id:    int
    report_type:  str
    grade_scales: list
    subjects_map: dict = field(default_factory=dict)
    papers_map:   dict = field(default_factory=dict)
    teacher_map:  dict = field(default_factory=dict)
    positions:    dict = field(default_factory=dict)
    total_students: int = 0

    @classmethod
    def build(
        cls,
        school_id:    int,
        stream_id:    int,
        term_id:      int,
        exam_enum,
        report_type:  str,
        grade_scales: list,
    ) -> "BatchContext":
        """Pre-load all stream-level data. Call once, before the student loop."""
        assessments = Assessment.query.filter(
            Assessment.school_id == school_id,
            Assessment.stream_id == stream_id,
            Assessment.term_id   == term_id,
            Assessment.type      == exam_enum,
        ).all()
        subject_ids = list({a.subject_id for a in assessments if a.subject_id})

        subjects_map = _load_subjects_map(subject_ids)
        papers_map   = _load_papers_map(subject_ids, school_id)
        teacher_map  = _load_teacher_map(school_id, stream_id, subject_ids)

        positions  = calculate_stream_positions(
            school_id, stream_id, term_id, exam_enum,
            grade_scales=grade_scales,
        )
        total_students = len(positions)

        return cls(
            school_id=school_id,
            stream_id=stream_id,
            report_type=report_type,
            grade_scales=grade_scales,
            subjects_map=subjects_map,
            papers_map=papers_map,
            teacher_map=teacher_map,
            positions=positions,
            total_students=total_students,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  LOCAL FILE STORAGE
# ─────────────────────────────────────────────────────────────────────────────

def ensure_report_dir(base_static: str, year_name: str, term_name: str, stream_name: str) -> str:
    def sanitise(s: str) -> str:
        return "".join(c for c in s if c.isalnum() or c in ("_", "-")).strip() or "unknown"

    path = os.path.join(
        base_static, "report_cards",
        sanitise(year_name), sanitise(term_name), sanitise(stream_name),
    )
    os.makedirs(path, exist_ok=True)
    return path


def html_to_pdf_bytes(html: str) -> tuple[bytes, str]:
    try:
        from weasyprint import HTML as WP
        pdf = WP(string=html).write_pdf()
        return pdf, "pdf"
    except ImportError:
        logger.warning("WeasyPrint not installed — storing HTML file instead of PDF.")
        return html.encode("utf-8"), "html"
    except Exception as exc:
        logger.error("WeasyPrint PDF conversion failed: %s", exc)
        return html.encode("utf-8"), "html"


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL-SPECIFIC TEMPLATE OVERRIDES
# ─────────────────────────────────────────────────────────────────────────────

_SCHOOL_TEMPLATE_OVERRIDES: dict[int, dict[str, str]] = {
    6: {  # Sunbay Junior School & Day Care Centre
        "nursery": "modules/academics/report_cards/sunbay_nursery_report_card.html",
        "primary": "modules/academics/report_cards/sunbay_primary_report_card.html",
    },
}

_SECTION_CATEGORY_MAP: dict[str, str] = {
    "nursery": "Nursery",
    "primary": "Primary",
    "olevel":  "O Level",
    "alevel":  "A Level",
}

_TEMPLATE_MAP: dict[str, str] = {
    "nursery": "modules/academics/report_cards/nursery_report_card.html",
    "primary": "modules/academics/report_cards/primary_report_card.html",
    "olevel":  "modules/academics/report_cards/olevel_report_card.html",
    "alevel":  "modules/academics/report_cards/alevel_report_card.html",
}


def get_template_name(school_id: int, report_type: str) -> str:
    """
    Return the Jinja template path to render for a given school + report_type.

    Resolution order:
      1. _SCHOOL_TEMPLATE_OVERRIDES[school_id][report_type], if present.
      2. _TEMPLATE_MAP[report_type] (shared default for that section).
    """
    overrides = _SCHOOL_TEMPLATE_OVERRIDES.get(school_id, {})
    return overrides.get(report_type, _TEMPLATE_MAP[report_type])


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN SERVICE CLASS
# ─────────────────────────────────────────────────────────────────────────────

class ReportCardService:
    """
    Orchestrates the full report-card generation pipeline for all four
    school sections: nursery, primary, olevel, alevel.

    Single-student usage
    ---------------------
        service = ReportCardService(school, school_detail)
        result  = service.generate(student, stream, term, year,
                                   exam_type, static_folder)

    Bulk usage (high-performance)
    --------------------------------
        service = ReportCardService(school, school_detail)
        batch   = service.build_batch_context(stream, term, year, exam_type)
        for student in students:
            result = service.generate(student, stream, term, year,
                                      exam_type, static_folder,
                                      batch_ctx=batch)
    """

    def __init__(self, school, school_detail):
        self.school        = school
        self.school_detail = school_detail

    def build_batch_context(
        self,
        stream:        "Stream",
        term:          "Term",
        academic_year: "AcademicYear",
        exam_type:     str,
    ) -> BatchContext:
        """
        Build a BatchContext for the given stream.  Pass it to every
        `generate()` call in the subsequent student loop to avoid repeated
        stream-level DB queries.
        """
        school_id   = self.school.id
        class_name  = stream.class_.name if stream and stream.class_ else ""
        report_type = classify_class(class_name)

        try:
            exam_enum = AssessmentType(exam_type.upper())
        except ValueError:
            raise ValueError(f"Invalid exam_type: {exam_type!r}. Must be BOT, MID or EOT.")

        grade_scales = fetch_grade_scales(school_id, _SECTION_CATEGORY_MAP.get(report_type))

        return BatchContext.build(
            school_id=school_id,
            stream_id=stream.id,
            term_id=term.id,
            exam_enum=exam_enum,
            report_type=report_type,
            grade_scales=grade_scales,
        )

    def generate(
        self,
        student:       "Student",
        stream:        "Stream",
        term:          "Term",
        academic_year: "AcademicYear",
        exam_type:     str,
        static_folder: str,
        batch_ctx:     Optional[BatchContext] = None,
    ) -> dict:
        """
        Generate a report card for one student.

        Parameters
        ----------
        batch_ctx : BatchContext, optional
            Pre-computed stream data.  Supply this when generating reports
            for multiple students in the same stream to share grade-scale,
            position, subject/paper/teacher lookups across all calls.

        Returns
        -------
        {
          "local_path":  str,
          "file_url":    str,
          "report_type": str,   # 'nursery' | 'primary' | 'olevel' | 'alevel'
          "extension":   str,   # 'pdf' | 'html'
        }
        """
        school_id   = self.school.id
        class_name  = stream.class_.name if stream and stream.class_ else ""
        report_type = classify_class(class_name)

        logger.info(
            "Generating report: student=%s class=%s report_type=%s exam=%s",
            student.id, class_name, report_type, exam_type,
        )

        # ── 1. Exam enum ─────────────────────────────────────────────────────
        try:
            exam_enum = AssessmentType(exam_type.upper())
        except ValueError:
            raise ValueError(f"Invalid exam_type: {exam_type!r}. Must be BOT, MID or EOT.")

        # ── 2. Grade scales ───────────────────────────────────────────────────
        if batch_ctx is not None:
            grade_scales = batch_ctx.grade_scales
        else:
            grade_scales = fetch_grade_scales(school_id, _SECTION_CATEGORY_MAP.get(report_type))

        # ── 3. Fetch marks ────────────────────────────────────────────────────
        marks_data = fetch_student_marks(
            school_id=school_id,
            student_id=student.id,
            term_id=term.id,
            exam_enum=exam_enum,
            stream_id=stream.id,
        )

        if not marks_data:
            logger.warning(
                "No marks for student=%s term=%s exam=%s — empty report will be generated.",
                student.id, term.id, exam_type,
            )

        # ── 4. Build subject rows ─────────────────────────────────────────────
        bulk_kwargs = {}
        if batch_ctx is not None:
            bulk_kwargs = {
                "subjects_map": batch_ctx.subjects_map,
                "papers_map":   batch_ctx.papers_map,
                "teacher_map":  batch_ctx.teacher_map,
            }

        if report_type in ("olevel", "alevel"):
            subject_rows = build_secondary_subject_rows(
                school_id=school_id,
                stream_id=stream.id,
                student_id=student.id,
                marks_data=marks_data,
                grade_scales=grade_scales,
                report_type=report_type,
                school_detail=self.school_detail,
                **bulk_kwargs,
            )
        else:
            subject_rows = build_subject_rows(
                school_id=school_id,
                stream_id=stream.id,
                student_id=student.id,
                marks_data=marks_data,
                grade_scales=grade_scales,
                report_type=report_type,
                **bulk_kwargs,
            )

        # ── 5. Aggregates / division / points ─────────────────────────────────
        aggregates        = None
        division          = None
        subsidiary_points = None

        if report_type == "primary":
            aggregates, division = compute_primary_aggregates(subject_rows, grade_scales)
        elif report_type == "olevel":
            aggregates, division = compute_olevel_aggregates(subject_rows, grade_scales)
        elif report_type == "alevel":
            aggregates, subsidiary_points = compute_alevel_points(subject_rows, grade_scales)

        # ── 6. Average mark ───────────────────────────────────────────────────
        score_key = "total_100" if report_type in ("olevel", "alevel") else "total_score"
        totals    = [r[score_key] for r in subject_rows if r.get(score_key) is not None]
        average_mark = round(sum(totals) / len(totals), 1) if totals else None

        # ── 7. Position ───────────────────────────────────────────────────────
        if batch_ctx is not None:
            position       = batch_ctx.positions.get(student.id)
            total_students = batch_ctx.total_students
        else:
            pos_map        = calculate_stream_positions(
                school_id, stream.id, term.id, exam_enum,
                grade_scales=grade_scales,
            )
            position       = pos_map.get(student.id)
            total_students = len(pos_map)

        # ── 8. Attendance ─────────────────────────────────────────────────────
        attendance = compute_attendance(school_id, student.id, term.id)

        # ── 9. Grade legend ───────────────────────────────────────────────────
        grade_legend = _build_grade_legend(grade_scales, report_type)

        # ── 10. Resolve absolute file:// URIs for WeasyPrint ─────────────────
        # url_for() is unavailable in background threads (no request context).
        # We use file:// URIs so WeasyPrint can load images directly from disk
        # without needing Flask routing or a running HTTP server.
        static_path    = Path(static_folder).resolve()
        sunbay_logo    = (static_path / "images" / "sunbay_logo.png").as_uri()
        children_image = (static_path / "images" / "children1.jpg").as_uri()
        static_url     = static_path.as_uri()

        # ── 11. Template context ──────────────────────────────────────────────
        ctx = {
            "school":             self.school,
            "school_detail":      self.school_detail,
            "student":            student,
            "learner_id":         student.admission_number or "",
            "stream":             stream,
            "class_name":         class_name,
            "stream_name":        stream.name if stream else "",
            "term":               term,
            "academic_year":      academic_year,
            "exam_type":          exam_type,
            "subject_rows":       subject_rows,
            "average_mark":       average_mark,
            "position":           position,
            "total_students":     total_students,
            "aggregates":         aggregates,
            "division":           division,
            "subsidiary_points":  subsidiary_points,
            "attendance":         attendance,
            "grade_legend":       grade_legend,
            "report_type":        report_type,
            "generated_date":     datetime.utcnow().strftime("%d %B %Y"),
            # ── file:// URIs for school-specific templates (no url_for needed) ──
            "sunbay_logo":        sunbay_logo,
            "children_image":     children_image,
            "static_url":         static_url,
            "static_folder":      static_path,   # kept for backward compat
        }

        # ── 12. Render HTML ───────────────────────────────────────────────────
        template_name = get_template_name(school_id, report_type)
        try:
            html = render_template(template_name, **ctx)
        except Exception as exc:
            logger.error(
                "Template rendering failed for student=%s template=%s: %s",
                student.id, template_name, exc,
            )
            raise RuntimeError(f"Template rendering failed: {exc}") from exc

        # ── 13. Convert to PDF ────────────────────────────────────────────────
        file_bytes, ext = html_to_pdf_bytes(html)

        # ── 14. Save to disk ──────────────────────────────────────────────────
        year_name    = academic_year.name if academic_year else "unknown"
        term_label   = (term.name or "term").replace(" ", "_") if term else "term"
        stream_label = (stream.name or "stream").replace(" ", "_") if stream else "stream"

        directory = ensure_report_dir(static_folder, year_name, term_label, stream_label)
        filename  = f"student_{student.id}_{exam_type.lower()}.{ext}"
        full_path = os.path.join(directory, filename)

        try:
            with open(full_path, "wb") as fh:
                fh.write(file_bytes)
            logger.info("Report written: %s (%d bytes)", full_path, len(file_bytes))
        except OSError as exc:
            raise RuntimeError(f"Could not write report file: {exc}") from exc

        # ── 15. Build URL path ────────────────────────────────────────────────
        try:
            rel      = os.path.relpath(full_path, os.path.dirname(static_folder))
            file_url = "/" + rel.replace("\\", "/")
        except ValueError:
            file_url = (
                f"/static/report_cards/{year_name}/{term_label}/{stream_label}/{filename}"
            )

        if not file_url.startswith("/static"):
            file_url = (
                f"/static/report_cards/{year_name}/{term_label}/{stream_label}/{filename}"
            )

        return {
            "local_path":  full_path,
            "file_url":    file_url,
            "report_type": report_type,
            "extension":   ext,
        }