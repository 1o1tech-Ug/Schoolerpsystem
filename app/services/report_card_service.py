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
  - sub_math_min_mark → Subsidiary Mathematics
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
template selection (see the `custom_reportcards` package and
`get_template_name()` below).

Section-category resolution (Nursery / Lower-Upper Primary / O-Level / A-Level)
----------------------------------------------------------------------------------
GradeScale rows are saved against a `section_category` string. For Primary,
schools configure separate scales for "Lower Primary" (P1-P3) and "Upper
Primary" (P4-P7) rather than a single "Primary" bucket. `resolve_section_category()`
maps a (report_type, class_name) pair to the exact category string to query,
and `fetch_grade_scales_for()` wraps `fetch_grade_scales()` with that
resolution (plus a fallback to a generic "Primary" bucket for schools that
haven't split their scale). All call sites that need a school's grade scale
should go through `fetch_grade_scales_for()` rather than calling
`fetch_grade_scales()` directly with a hardcoded/guessed category string.

Primary aggregate-subject restriction
---------------------------------------
Per the Uganda primary curriculum, only a fixed set of "core" subjects
counts toward a primary learner's aggregates/division — every other
subject is still graded and shown on the report, but excluded from the
aggregate sum:

  P1 – P3  →  Literacy I, Literacy II, Mathematics, English
  P4 – P7  →  Mathematics, English, Social Studies, Science

This filtering is applied via `filter_rows_for_primary_aggregates()`
before `compute_primary_aggregates()` is called, both in the per-student
report pipeline (`ReportCardService.generate()`) and in stream-wide
ranking (`calculate_stream_positions()`), so aggregates and class
positions stay consistent with each other.

Primary EOT reports (MID + EOT averaging)
--------------------------------------------
For Primary, the End-of-Term (EOT) report shows MID marks, EOT marks,
and a FINAL MARKS value per subject, where FINAL MARKS is the rounded
average of the two (if only one of the two exists for a subject, that
score is used directly as the final mark). This is handled by
`build_eot_subject_rows()`, which is used instead of `build_subject_rows()`
whenever `report_type == "primary"` and `exam_type == "EOT"`. Grading,
aggregates, and the grading-legend table all key off this rounded final
mark exactly as they would off a normal single-exam score.

School-specific templates
--------------------------
Individual schools that need a bespoke report-card layout do NOT have
their overrides hardcoded in this file. Each such school's
customizations live in their own module under
`app/services/custom_reportcards/` (e.g. `custom_reportcards/sunbay.py`),
which self-registers its template overrides at import time via
`register_school()`. This file only ever talks to that registry — it
has no knowledge of which schools exist or how many there are. See
`app/services/custom_reportcards/__init__.py` for the registration
contract and step-by-step instructions for adding a new school.

An override entry can be either:
  - a plain template path string (used for every exam type), or
  - a dict keyed by exam type ("BOT" / "MID" / "EOT") mapping to a
    template path, letting a school use a different layout for its
    EOT report than for its BOT/MID reports (e.g. a primary section
    that shows MID+EOT+FINAL columns only on the EOT report).

`get_template_name()` resolves which template a given
(school_id, report_type, exam_type) combination should render by
looking up `custom_reportcards.get_overrides(school_id)`. Adding a new
school's custom layout is therefore just a matter of adding one new
module under `custom_reportcards/` — no changes to this file required.

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

Error-logging notes
--------------------
- Every `except` block in the generation pipeline now logs via
  `logger.exception(...)` (or `logger.error(..., exc_info=True)`), so the
  full Python traceback — not just the exception message — is written to
  stdout/stderr. On Render's free tier this is what shows up in the
  "Logs" tab, since there's no separate error-tracking service wired up.
- `ReportCardService.generate()` wraps its entire body in a top-level
  try/except. Whatever step fails (marks fetch, subject-row building,
  aggregate computation, template rendering, PDF conversion, disk write),
  the full traceback is logged with the student/stream/exam context
  *before* the error propagates, so a generic "failed to render" caught
  further up the call stack no longer hides where it came from.
"""

import os
import re as _re
import logging
import traceback
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
    StudentMark, StudentStream,
    StudentDailyAttendance,
    GradeScale,
)
from app.models.reportcards import PrimaryReportSummary
from app.services.custom_reportcards import get_overrides as get_school_report_overrides

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
#  PRIMARY AGGREGATE-SUBJECT RESTRICTION  (P1-P3 vs P4-P7)
# ─────────────────────────────────────────────────────────────────────────────

_LOWER_PRIMARY_CLASS_PREFIXES = ("p1", "p2", "p3")
_UPPER_PRIMARY_CLASS_PREFIXES = ("p4", "p5", "p6", "p7")

# Normalised subject "keys" — see _primary_aggregate_subject_key() below.
_LOWER_PRIMARY_AGG_KEYS = frozenset({"english", "mathematics", "literacy_1", "literacy_2"})
_UPPER_PRIMARY_AGG_KEYS = frozenset({"english", "mathematics", "social_studies", "science"})

_LIT1_PAT = _re.compile(r'^literacy\s*(1|i)\b', _re.IGNORECASE)
_LIT2_PAT = _re.compile(r'^literacy\s*(2|ii)\b', _re.IGNORECASE)


def _primary_aggregate_subject_key(subject_name: str) -> Optional[str]:
    """
    Normalise a subject name into one of the fixed keys used to decide
    whether it counts toward a primary learner's aggregate/division:

        'english', 'mathematics', 'literacy_1', 'literacy_2',
        'social_studies', 'science'

    Returns None for any subject that doesn't match one of these —
    such subjects are still graded and shown on the report card, they
    just don't contribute to the aggregate sum.
    """
    name = (subject_name or "").strip().lower()

    if _LIT2_PAT.match(name):
        return "literacy_2"
    if _LIT1_PAT.match(name):
        return "literacy_1"
    if name.startswith("english"):
        return "english"
    if name.startswith("math"):
        return "mathematics"
    if name.startswith("social studies") or name.startswith("social_studies"):
        return "social_studies"
    if name.startswith("science"):
        return "science"
    return None


def _primary_aggregate_keys_for_class(class_name: str) -> frozenset:
    """
    Return the set of aggregate-subject keys applicable to *class_name*:
      P1–P3 → English, Mathematics, Literacy I, Literacy II
      P4–P7 → English, Mathematics, Social Studies, Science
    Unrecognised primary class names fall back to the upper-primary set
    (with a warning logged) rather than silently including every subject.
    """
    normalised = (class_name or "").strip().lower()

    for prefix in _LOWER_PRIMARY_CLASS_PREFIXES:
        if normalised.startswith(prefix):
            return _LOWER_PRIMARY_AGG_KEYS

    for prefix in _UPPER_PRIMARY_CLASS_PREFIXES:
        if normalised.startswith(prefix):
            return _UPPER_PRIMARY_AGG_KEYS

    logger.warning(
        "Unrecognised primary class name '%s' for aggregate-subject "
        "filtering; defaulting to the P4-P7 subject set.", class_name,
    )
    return _UPPER_PRIMARY_AGG_KEYS


def filter_rows_for_primary_aggregates(subject_rows: list, class_name: str) -> list:
    """
    Return the subset of *subject_rows* that should count toward a
    primary learner's aggregate/division, based on their specific class:

      P1–P3  →  Literacy I, Literacy II, Mathematics, English
      P4–P7  →  Mathematics, English, Social Studies, Science

    Used by calculate_stream_positions() (ranking only needs the filtered
    list, not display ordering). For the report card itself, use
    _finalize_primary_subject_rows() instead, which both filters *and*
    reorders/blanks the display rows.
    """
    allowed_keys = _primary_aggregate_keys_for_class(class_name)
    filtered = []
    for row in subject_rows:
        key = _primary_aggregate_subject_key(row.get("subject_name", ""))
        if key is not None and key in allowed_keys:
            filtered.append(row)
    return filtered


def _finalize_primary_subject_rows(subject_rows: list, class_name: str) -> list:
    """
    Prepare a primary report's subject rows for display and aggregation:

      - Every row is tagged with "is_aggregate_subject" (True/False).
      - Rows for subjects OUTSIDE the class's core aggregate set (see
        filter_rows_for_primary_aggregates()) have their "grade" and
        "remark" cleared to "" — these subjects are still shown with
        their score, but curriculum convention doesn't assign them a
        formal grade/remark on this report.
      - Core aggregate-subject rows are listed first (alphabetically),
        followed by non-core rows (also alphabetically) at the bottom
        of the table.

    Pass the *core* rows (subject_rows where is_aggregate_subject is
    True) into compute_primary_aggregates() — this function itself only
    reorders and adjusts display fields, it doesn't compute grades.
    """
    allowed_keys = _primary_aggregate_keys_for_class(class_name)

    core_rows: list = []
    other_rows: list = []

    for row in subject_rows:
        key = _primary_aggregate_subject_key(row.get("subject_name", ""))
        if key is not None and key in allowed_keys:
            row["is_aggregate_subject"] = True
            core_rows.append(row)
        else:
            row["is_aggregate_subject"] = False
            row["grade"]  = ""
            row["remark"] = ""
            other_rows.append(row)

    core_rows.sort(key=lambda r: r["subject_name"])
    other_rows.sort(key=lambda r: r["subject_name"])
    return core_rows + other_rows


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

# Ordered worst-ward so a "push to next division" is just "move one step
# to the right", capping at Ungraded (U) — an already-ungraded learner
# can't be pushed any further.
_PRIMARY_DIVISION_ORDER = [
    "Division 1", "Division 2", "Division 3", "Division 4", "Ungraded (U)",
]


def compute_primary_division(aggregates: int, force_next_division: bool = False) -> str:
    """
    Return the Primary division for *aggregates*.

    If *force_next_division* is True (an F9 in English or Mathematics —
    see _has_f9_in_english_or_math()), the learner is pushed one division
    worse than their aggregate alone would indicate, per the standard
    Uganda primary-leaving rule. A learner already at "Ungraded (U)"
    stays there.
    """
    if aggregates <= _PRIMARY_DIV1_MAX:
        division = "Division 1"
    elif aggregates <= _PRIMARY_DIV2_MAX:
        division = "Division 2"
    elif aggregates <= _PRIMARY_DIV3_MAX:
        division = "Division 3"
    elif aggregates <= _PRIMARY_DIV4_MAX:
        division = "Division 4"
    else:
        division = "Ungraded (U)"

    if force_next_division and division != "Ungraded (U)":
        idx = _PRIMARY_DIVISION_ORDER.index(division)
        division = _PRIMARY_DIVISION_ORDER[idx + 1]

    return division


def _has_f9_in_english_or_math(aggregate_rows: list) -> bool:
    """
    Return True if English or Mathematics graded F9 among *aggregate_rows*
    (the core-subject rows used for aggregation). An F9 in either subject
    automatically pushes a Primary learner to the next Division, per
    curriculum convention, regardless of what their aggregate total
    alone would otherwise give them.
    """
    for row in aggregate_rows:
        key = _primary_aggregate_subject_key(row.get("subject_name", ""))
        if key in ("english", "mathematics") and (row.get("grade") or "").strip().upper() == "F9":
            return True
    return False


def compute_primary_aggregates(
    subject_rows: list,
    grade_scales: list,
    force_next_division: bool = False,
) -> tuple[Optional[int], Optional[str]]:
    """
    Compute Primary aggregates/division from *subject_rows*.

    IMPORTANT: callers must pass in only the subjects that should count
    toward the aggregate — i.e. the core-subject rows produced by
    _finalize_primary_subject_rows() / filter_rows_for_primary_aggregates()
    — not every subject on the report card. This function itself just
    sums the grade points of whatever rows it's given (best 4, in case
    more than 4 are passed in for a school with an unusual subject list).

    Pass force_next_division=True (see _has_f9_in_english_or_math()) to
    apply the "F9 in English or Math pushes you to the next Division" rule.
    """
    if not subject_rows:
        return None, None
    grade_point_list = [
        PRIMARY_GRADE_POINTS.get(row.get("grade", "F9"), 9)
        for row in subject_rows
    ]
    if not grade_point_list:
        return None, None
    aggregates = sum(sorted(grade_point_list)[:4])
    return aggregates, compute_primary_division(aggregates, force_next_division=force_next_division)


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
    category string (exact match), ordered highest-first.

    An empty list here means "no GradeScale rows exist for that exact
    section_category" — every grade_fn / _build_grade_legend() call
    interprets an empty list as "use the Uganda-standard defaults".

    NOTE: section_category must match the DB value exactly (e.g.
    "Lower Primary", "Upper Primary", "Nursery", "O Level", "A Level").
    Most callers should go through fetch_grade_scales_for() instead of
    calling this directly, since that function resolves the correct
    category string (including the Lower/Upper Primary split) for you.
    """
    q = GradeScale.query.filter_by(school_id=school_id)
    if section_category:
        q = q.filter_by(section_category=section_category)
    return q.order_by(GradeScale.min_score.desc()).all()


def resolve_section_category(report_type: str, class_name: str = "") -> str:
    """
    Map (report_type, class_name) to the exact GradeScale.section_category
    string used in the database.

        'nursery' -> 'Nursery'
        'primary' -> 'Lower Primary' (P1-P3) or 'Upper Primary' (P4-P7),
                     based on the same class-name split already used for
                     aggregate-subject filtering
        'olevel'  -> 'O Level'
        'alevel'  -> 'A Level'

    Any other/unrecognised report_type falls back to 'Primary', matching
    the historical (pre-split) behaviour.
    """
    if report_type == "primary":
        keys = _primary_aggregate_keys_for_class(class_name)
        return "Lower Primary" if keys is _LOWER_PRIMARY_AGG_KEYS else "Upper Primary"
    return _SECTION_CATEGORY_MAP.get(report_type, "Primary")


def fetch_grade_scales_for(school_id: int, report_type: str, class_name: str = "") -> list:
    """
    Resolve the correct section_category for (report_type, class_name) via
    resolve_section_category(), then fetch that school's GradeScale rows.

    For primary, if no "Lower Primary"/"Upper Primary"-specific rows exist,
    falls back to a generic "Primary" category — so schools that haven't
    split their scale into lower/upper bands yet keep working exactly as
    before. This is the entry point every call site should use instead of
    calling fetch_grade_scales() directly with a guessed category string.
    """
    category = resolve_section_category(report_type, class_name)
    scales = fetch_grade_scales(school_id, category)
    if not scales and report_type == "primary":
        scales = fetch_grade_scales(school_id, "Primary")
    return scales


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

    Reads from StudentDailyAttendance — one row per student per calendar
    day (see _sync_daily_attendance() in teachers_apis.py) — rather than
    the raw per-lesson StudentAttendance table. This is what makes "DAYS
    ATTENDED" / "ABSENT" / "TOT" on the report card actually mean days,
    not individual subject lessons.

    A day counts if it falls within the term's [start_date, end_date]
    range (inclusive).
    """
    try:
        term = Term.query.get(term_id)
        if not term or not term.start_date or not term.end_date:
            logger.warning(
                "compute_attendance: term=%s has no start/end date configured "
                "— returning zeroed attendance.", term_id,
            )
            return {"total": 0, "present": 0, "absent": 0}

        row = db.session.execute(
            select(
                func.count(StudentDailyAttendance.id).label("total"),
                func.sum(
                    case((StudentDailyAttendance.status == "present", 1), else_=0)
                ).label("present"),
            ).where(
                StudentDailyAttendance.school_id  == school_id,
                StudentDailyAttendance.student_id == student_id,
                StudentDailyAttendance.date >= term.start_date,
                StudentDailyAttendance.date <= term.end_date,
            )
        ).one()

        total   = int(row.total   or 0)
        present = int(row.present or 0)
        return {"total": total, "present": present, "absent": total - present}

    except Exception:
        logger.exception(
            "compute_attendance failed for student=%s term=%s",
            student_id, term_id,
        )
        return {"total": 0, "present": 0, "absent": 0}

# ─────────────────────────────────────────────────────────────────────────────
#  SHARED PER-SUBJECT PERCENTAGE HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _compute_subject_pct(paper_scores: dict, papers: list) -> Optional[float]:
    """
    Normalise one subject's raw marks into a 0-100 percentage.

    If the subject has registered papers, sums score/max across all
    papers that have a recorded score and returns the percentage of the
    total. If the subject has no papers, treats the single stored score
    (keyed by None) as already being out of 100.

    Returns None if there's no recorded score at all.
    """
    if papers:
        total_score = 0.0
        total_max   = 0.0
        for paper in papers:
            score = paper_scores.get(paper.id)
            max_m = float(paper.max_marks) if paper.max_marks else 100.0
            if score is not None:
                total_score += score
                total_max   += max_m
        if total_max > 0:
            return (total_score / total_max) * 100.0
        return None
    else:
        score = paper_scores.get(None)
        return float(score) if score is not None else None


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
    Build subject row dicts for primary / nursery templates (single-exam
    report: BOT or MID). For the primary EOT report, use
    build_eot_subject_rows() instead.

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
#  PRIMARY EOT SUBJECT ROWS BUILDER  (MID + EOT → averaged FINAL MARKS)
# ─────────────────────────────────────────────────────────────────────────────

def build_eot_subject_rows(
    school_id:      int,
    stream_id:      int,
    student_id:     int,
    mid_marks_data: dict,
    eot_marks_data: dict,
    grade_scales:   list,
    report_type:    str,
    subjects_map: Optional[dict] = None,
    papers_map:   Optional[dict] = None,
    teacher_map:  Optional[dict] = None,
) -> list:
    """
    Build subject row dicts for the Primary End-of-Term (EOT) report,
    which shows MID marks, EOT marks, and a FINAL MARKS value per subject.

    FINAL MARKS = round(average of whichever of {mid %, eot %} are
    recorded). If only one of the two is recorded for a subject, that
    score alone is used as the final mark (nothing to average against).
    If neither is recorded, the subject is listed with '—' placeholders
    and excluded from grading (grade "—").

    Grading, aggregates, and the grading-legend table all key off the
    resulting "total_score" field exactly as they would for a normal
    single-exam report, so this row list is a drop-in replacement for
    build_subject_rows()'s output wherever a primary EOT report is
    being generated.
    """
    subject_ids = sorted(set(mid_marks_data.keys()) | set(eot_marks_data.keys()))

    if subjects_map is None:
        subjects_map = _load_subjects_map(subject_ids)
    if papers_map is None:
        papers_map = _load_papers_map(subject_ids, school_id)
    if teacher_map is None:
        teacher_map = _load_teacher_map(school_id, stream_id, subject_ids)

    grade_fn = nursery_grade if report_type == "nursery" else primary_grade

    rows = []
    for subj_id in subject_ids:
        subject = subjects_map.get(subj_id)
        if not subject:
            continue

        teacher_name = teacher_map.get(subj_id, "")
        papers       = papers_map.get(subj_id, [])

        mid_pct = _compute_subject_pct(mid_marks_data.get(subj_id, {}), papers)
        eot_pct = _compute_subject_pct(eot_marks_data.get(subj_id, {}), papers)

        available = [p for p in (mid_pct, eot_pct) if p is not None]
        final_pct = round(sum(available) / len(available)) if available else None

        if final_pct is not None:
            grade, remark = grade_fn(final_pct, grade_scales)
        else:
            grade, remark = "—", "—"

        rows.append({
            "subject_name": subject.name,
            "teacher_name": teacher_name,
            "mid_score":    round(mid_pct, 1) if mid_pct is not None else None,
            "eot_score":    round(eot_pct, 1) if eot_pct is not None else None,
            # Kept as "total_score" (not "final_score") so that
            # compute_primary_aggregates(), the average-mark calculation,
            # and the grade legend all work unchanged for EOT rows.
            "total_score":  final_pct,
            "grade":        grade,
            "remark":       remark,
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

    # Needed only so that primary aggregate filtering can match rows by
    # subject name (see filter_rows_for_primary_aggregates() below).
    subjects_map_local = (
        _load_subjects_map(subject_ids_in_play) if report_type == "primary" else {}
    )

    if grade_scales is None:
        # Resolve the correct section_category for this stream's report
        # type (and, for primary, its Lower/Upper split) instead of
        # pulling every GradeScale row the school has configured across
        # all sections.
        grade_scales = fetch_grade_scales_for(school_id, report_type, class_name)

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
            subject_name    = (
                subjects_map_local.get(subj_id).name
                if subjects_map_local.get(subj_id) else ""
            )

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
                    subject_rows_local.append({
                        "total_score": round(pct, 1),
                        "grade": grade,
                        "subject_name": subject_name,
                    })
            else:
                sc = paper_scores.get(None)
                if sc is not None:
                    grade, _ = grade_fn(float(sc), grade_scales)
                    subject_rows_local.append({
                        "total_score": round(float(sc), 1),
                        "grade": grade,
                        "subject_name": subject_name,
                    })
                    raw_totals.append(sc)

        if report_type == "primary" and subject_rows_local:
            aggregate_rows = filter_rows_for_primary_aggregates(subject_rows_local, class_name)
            agg, _ = compute_primary_aggregates(aggregate_rows, grade_scales)
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
    except Exception:
        # Full traceback (e.g. missing system libs like libpango/libcairo,
        # which is a common failure mode on Render's free tier where those
        # aren't pre-installed) goes to the log here.
        logger.exception("WeasyPrint PDF conversion failed")
        return html.encode("utf-8"), "html"


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL-SPECIFIC TEMPLATE OVERRIDES
# ─────────────────────────────────────────────────────────────────────────────
#
# Per-school template customizations no longer live in this file. Each
# school that needs a bespoke layout has its own module under
# app/services/custom_reportcards/ (e.g. custom_reportcards/sunbay.py),
# which self-registers with the registry at import time. This file talks
# to that registry via get_school_report_overrides() (imported at the
# top of this module) and never hardcodes a school_id anywhere below.
#
# See app/services/custom_reportcards/__init__.py for the full contract
# and instructions for adding a new school's custom templates.

# Base section-category strings for sections that are NOT split by
# lower/upper (Nursery, O Level, A Level). Primary is handled separately
# by resolve_section_category() since it's split into "Lower Primary" /
# "Upper Primary".
_SECTION_CATEGORY_MAP: dict[str, str] = {
    "nursery": "Nursery",
    "olevel":  "O Level",
    "alevel":  "A Level",
}

_TEMPLATE_MAP: dict[str, str] = {
    "nursery": "modules/academics/report_cards/nursery_report_card.html",
    "primary": "modules/academics/report_cards/primary_report_card.html",
    "olevel":  "modules/academics/report_cards/olevel_report_card.html",
    "alevel":  "modules/academics/report_cards/alevel_report_card.html",
}


def get_template_name(school_id: int, report_type: str, exam_type: Optional[str] = None) -> str:
    """
    Return the Jinja template path to render for a given
    (school_id, report_type, exam_type) combination.

    Resolution order:
      1. custom_reportcards.get_overrides(school_id)[report_type] — the
         school-specific override registered under
         app/services/custom_reportcards/ (one module per school):
           - if that's a dict (keyed by exam type), look up exam_type
             there, falling back to the shared default template for that
             section if the exam type isn't listed;
           - if that's a plain string, it's used regardless of exam_type
             (the school hasn't split its templates by exam type).
      2. _TEMPLATE_MAP[report_type] (system default for that section).

    This function deliberately has zero knowledge of which schools have
    custom templates — that's entirely owned by the custom_reportcards
    registry — so this file never needs to change as new schools with
    bespoke layouts are added.
    """
    overrides = get_school_report_overrides(school_id)
    override  = overrides.get(report_type)

    if isinstance(override, dict):
        exam_key = (exam_type or "").upper()
        return override.get(exam_key, _TEMPLATE_MAP[report_type])
    if override:
        return override
    return _TEMPLATE_MAP[report_type]


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

        grade_scales = fetch_grade_scales_for(school_id, report_type, class_name)

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

        Raises
        ------
        Whatever the underlying failure was (ValueError for a bad exam_type,
        RuntimeError for template/file failures, or any unexpected exception
        from the DB/computation steps). Before re-raising, the full
        traceback plus student/stream/exam context is written to the log
        via logger.error(..., exc_info=True), so it appears in Render logs
        even though the caller ultimately just sees "failed to render".
        """
        school_id   = self.school.id
        class_name  = stream.class_.name if stream and stream.class_ else ""
        report_type = classify_class(class_name)

        logger.info(
            "Generating report: student=%s class=%s report_type=%s exam=%s",
            student.id, class_name, report_type, exam_type,
        )

        try:
            # ── 1. Exam enum ─────────────────────────────────────────────────
            try:
                exam_enum = AssessmentType(exam_type.upper())
            except ValueError:
                raise ValueError(f"Invalid exam_type: {exam_type!r}. Must be BOT, MID or EOT.")

            # ── 2. Grade scales ───────────────────────────────────────────────
            if batch_ctx is not None:
                grade_scales = batch_ctx.grade_scales
            else:
                grade_scales = fetch_grade_scales_for(school_id, report_type, class_name)

            # ── 3. Fetch marks ────────────────────────────────────────────────
            # Primary EOT reports need BOTH the MID and EOT marks, since the
            # report averages them into a FINAL MARKS column.
            is_primary_eot = (report_type == "primary" and exam_type.upper() == "EOT")

            mid_marks_data: dict = {}
            eot_marks_data: dict = {}

            if is_primary_eot:
                mid_marks_data = fetch_student_marks(
                    school_id=school_id,
                    student_id=student.id,
                    term_id=term.id,
                    exam_enum=AssessmentType("MID"),
                    stream_id=stream.id,
                )
                eot_marks_data = fetch_student_marks(
                    school_id=school_id,
                    student_id=student.id,
                    term_id=term.id,
                    exam_enum=exam_enum,
                    stream_id=stream.id,
                )
                marks_data = eot_marks_data  # used only for the "no marks" check below

                if not mid_marks_data and not eot_marks_data:
                    logger.warning(
                        "No MID or EOT marks for student=%s term=%s — empty report will be generated.",
                        student.id, term.id,
                    )
            else:
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

            # ── 4. Build subject rows ─────────────────────────────────────────
            bulk_kwargs = {}
            if batch_ctx is not None:
                bulk_kwargs = {
                    "subjects_map": batch_ctx.subjects_map,
                    "papers_map":   batch_ctx.papers_map,
                    "teacher_map":  batch_ctx.teacher_map,
                }

            if is_primary_eot:
                subject_rows = build_eot_subject_rows(
                    school_id=school_id,
                    stream_id=stream.id,
                    student_id=student.id,
                    mid_marks_data=mid_marks_data,
                    eot_marks_data=eot_marks_data,
                    grade_scales=grade_scales,
                    report_type=report_type,
                    **bulk_kwargs,
                )
            elif report_type in ("olevel", "alevel"):
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

            # ── 5. Aggregates / division / points ─────────────────────────────
            aggregates        = None
            division          = None
            subsidiary_points = None

            if report_type == "primary":
                # Reorder so the core aggregate subjects (P1-P3: Literacy
                # I/II, Math, English — P4-P7: Math, English, Social
                # Studies, Science) come first, non-core subjects last
                # with their grade/remark blanked out. See
                # _finalize_primary_subject_rows() docstring for detail.
                subject_rows = _finalize_primary_subject_rows(subject_rows, class_name)
                aggregate_rows = [r for r in subject_rows if r["is_aggregate_subject"]]

                # An F9 in English or Mathematics pushes the learner to
                # the next Division regardless of their aggregate total.
                force_next_division = _has_f9_in_english_or_math(aggregate_rows)
                aggregates, division = compute_primary_aggregates(
                    aggregate_rows, grade_scales,
                    force_next_division=force_next_division,
                )
            elif report_type == "olevel":
                aggregates, division = compute_olevel_aggregates(subject_rows, grade_scales)
            elif report_type == "alevel":
                aggregates, subsidiary_points = compute_alevel_points(subject_rows, grade_scales)

            # ── 6. Average mark ─────────────────────────────────────────────
            score_key = "total_100" if report_type in ("olevel", "alevel") else "total_score"
            totals    = [r[score_key] for r in subject_rows if r.get(score_key) is not None]
            average_mark = round(sum(totals) / len(totals), 1) if totals else None

            # ── 7. Position ───────────────────────────────────────────────────
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

            # ── 8. Attendance ─────────────────────────────────────────────────
            attendance = compute_attendance(school_id, student.id, term.id)

            # ── 9. Grade legend ───────────────────────────────────────────────
            grade_legend = _build_grade_legend(grade_scales, report_type)

            # ── 10. Resolve absolute file:// URIs for WeasyPrint ───────────────
            # url_for() is unavailable in background threads (no request context).
            # We use file:// URIs so WeasyPrint can load images directly from disk
            # without needing Flask routing or a running HTTP server.
            static_path    = Path(static_folder).resolve()
            static_url     = static_path.as_uri()
            # `sunbay_logo` / `children_image` are kept as named context
            # keys solely for backward compatibility with Sunbay's
            # existing custom_reportcards templates, which already
            # reference these variable names. Any *new* custom template
            # (see app/services/custom_reportcards/) should build its own
            # image URIs from `static_url` above rather than adding more
            # one-off named keys here.
            sunbay_logo    = (static_path / "images" / "sunbay_logo.png").as_uri()
            children_image = (static_path / "images" / "children1.jpg").as_uri()

            # ── 11. Template context ────────────────────────────────────────
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

            # ── 12. Render HTML ─────────────────────────────────────────────
            template_name = get_template_name(school_id, report_type, exam_type)
            try:
                html = render_template(template_name, **ctx)
            except Exception as exc:
                # exc_info=True writes the full traceback (including which
                # Jinja file/line raised, e.g. a missing/renamed variable)
                # to the log — this is especially useful when the failing
                # template is a school-specific override registered under
                # app/services/custom_reportcards/, since those may
                # reference context keys that differ from the default
                # template's.
                logger.error(
                    "Template rendering failed for student=%s school=%s "
                    "report_type=%s exam_type=%s template=%s",
                    student.id, school_id, report_type, exam_type, template_name,
                    exc_info=True,
                )
                raise RuntimeError(f"Template rendering failed: {exc}") from exc

            # ── 13. Convert to PDF ───────────────────────────────────────────
            file_bytes, ext = html_to_pdf_bytes(html)

            # ── 14. Save to disk ──────────────────────────────────────────────
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
                logger.error(
                    "Could not write report file for student=%s path=%s",
                    student.id, full_path, exc_info=True,
                )
                raise RuntimeError(f"Could not write report file: {exc}") from exc

            # ── 15. Build URL path ────────────────────────────────────────────
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

        except Exception:
            # ── Top-level safety net ──────────────────────────────────────────
            # Whatever step above failed (marks fetch, aggregate math, grading,
            # template render, PDF conversion, disk write) — this guarantees
            # the FULL traceback lands in the log, with enough context to
            # locate it, *before* the exception propagates up to whatever
            # route/view catches it and shows the generic "failed to render"
            # message to the user.
            #
            # traceback.format_exc() is captured explicitly (in addition to
            # exc_info=True) so you have a plain string version too, in case
            # your logging handler/formatter on Render doesn't render
            # exc_info nicely.
            tb_str = traceback.format_exc()
            logger.error(
                "generate() failed: student=%s school=%s stream=%s term=%s "
                "exam=%s report_type=%s\n%s",
                getattr(student, "id", None),
                school_id,
                getattr(stream, "id", None),
                getattr(term, "id", None),
                exam_type,
                report_type,
                tb_str,
                exc_info=True,
            )
            raise