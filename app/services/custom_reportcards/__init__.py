"""
app/services/custom_reportcards/
==================================
One module per school that needs a bespoke report-card layout.

This package exists so that `report_card_service.py` — shared by every
school on the platform — never has to grow a new hardcoded block every
time a school asks for a custom design. Instead:

  - Each school gets its own file here (e.g. `sunbay.py`).
  - That file calls `register_school(school_id, overrides)` at import
    time to register its template paths.
  - `report_card_service.get_template_name()` looks the school up via
    `get_overrides()` and falls back to the shared default templates
    for any report_type/exam_type the school hasn't overridden.

See `registry.py` for the full contract, override-value shape, and
step-by-step instructions for adding a new school.

Public API
-----------
    from app.services.custom_reportcards import register_school, get_overrides

    register_school(school_id, {...})   # called by each school's module
    get_overrides(school_id) -> dict    # called by report_card_service.py
"""

from .registry import register_school, get_overrides, registered_school_ids

__all__ = ["register_school", "get_overrides", "registered_school_ids"]