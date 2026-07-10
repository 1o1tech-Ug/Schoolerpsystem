"""
app/services/custom_reportcards/sunbay.py
============================================
Sunbay Junior School & Day Care Centre — custom report-card templates.

school_id: 6

Sunbay uses its own nursery report-card design, and its own primary
report-card design — with a further split on primary: the End-of-Term
(EOT) report shows MID marks, EOT marks, and a FINAL MARKS column
(the rounded average of the two, per build_eot_subject_rows() in
report_card_service.py), so EOT gets its own template distinct from
the BOT/MID layout.

Sunbay is graded using the same GradeScale-driven logic as every other
school (see fetch_grade_scales_for() in report_card_service.py) — this
module only overrides which .html template gets rendered, nothing about
how grades/aggregates/divisions are computed.
"""

from .registry import register_school

SCHOOL_ID = 6

register_school(SCHOOL_ID, {
    "nursery": "modules/academics/report_cards/Sunbay_nursery_report_card.html",
    "primary": {
        "BOT": "modules/academics/report_cards/sunbay_primary_report_card.html",
        "MID": "modules/academics/report_cards/sunbay_primary_report_card.html",
        "EOT": "modules/academics/report_cards/sunbay_primary_eot_report_card.html",
    },
})