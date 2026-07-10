"""
app/services/custom_reportcards/registry.py
=============================================
Central registry for per-school report-card template overrides.

Why this exists
-----------------
`report_card_service.py` is shared by every school on the platform. As
more schools ask for a bespoke report-card layout, hardcoding each one
into that file turns it into a dumping ground that every unrelated
change has to scroll past — and risks a typo in one school's block
breaking template resolution for everybody else.

Instead, each school's customizations live in their own module under
`app/services/custom_reportcards/` (e.g. `sunbay.py`). That module
calls `register_school()` once, at import time, to register its
template paths here. `report_card_service.get_template_name()` only
ever talks to this registry — it has no knowledge of which schools
exist or how many there are.

Adding a new school
---------------------
1. Create `app/services/custom_reportcards/<school_slug>.py`.
2. In it, call:

       from .registry import register_school

       register_school(<school_id>, {
           "primary": "modules/academics/report_cards/<slug>/primary.html",
           ...
       })

3. Put that school's custom .html templates somewhere under
   `templates/modules/academics/report_cards/` (a per-school
   subfolder is recommended once you have more than a couple of
   custom schools, to keep the templates directory navigable).
4. Nothing else. The registry auto-discovers every module in this
   package the first time a template lookup happens — no imports to
   wire up in report_card_service.py, __init__.py, or anywhere else.

Override value shape
----------------------
Each report_type key ("nursery" / "primary" / "olevel" / "alevel") maps
to either:
  - a plain template path string, used for every exam type, or
  - a dict keyed by exam type ("BOT" / "MID" / "EOT") mapping to a
    template path, for schools whose EOT layout differs from their
    BOT/MID layout (e.g. a primary section that shows MID+EOT+FINAL
    columns only on the EOT report).
"""

import importlib
import pkgutil
import logging
from typing import Union

logger = logging.getLogger(__name__)

TemplateOverride = Union[str, dict[str, str]]

# school_id -> { report_type: TemplateOverride }
_REGISTRY: dict[int, dict[str, TemplateOverride]] = {}
_loaded = False


def register_school(school_id: int, overrides: dict[str, TemplateOverride]) -> None:
    """
    Register template overrides for a school.

    Called once, at import time, by that school's module in this
    package. If the same school_id is registered more than once (e.g.
    a stray duplicate module), the overrides are merged and a warning
    is logged rather than one registration silently clobbering the
    other.
    """
    if school_id in _REGISTRY:
        logger.warning(
            "custom_reportcards: school_id=%s registered more than once; "
            "merging overrides (later registration wins on key conflicts).",
            school_id,
        )
        _REGISTRY[school_id].update(overrides)
    else:
        _REGISTRY[school_id] = dict(overrides)


def _load_all() -> None:
    """
    Import every sibling module in this package exactly once, so each
    school's register_school() call has run. Safe to call repeatedly —
    only does real work the first time.
    """
    global _loaded
    if _loaded:
        return

    package = importlib.import_module(__package__)
    for _, module_name, is_pkg in pkgutil.iter_modules(package.__path__):
        if is_pkg or module_name == "registry":
            continue
        try:
            importlib.import_module(f"{__package__}.{module_name}")
        except Exception:
            # A broken school module should never take down report-card
            # generation for every other school on the platform — log
            # loudly and keep going so the rest still register.
            logger.exception(
                "custom_reportcards: failed to import module '%s' — "
                "that school's custom templates will NOT be available "
                "until this is fixed.",
                module_name,
            )

    _loaded = True


def get_overrides(school_id: int) -> dict[str, TemplateOverride]:
    """
    Return the registered template overrides for a school, or {} if the
    school has no custom report-card templates registered.
    """
    _load_all()
    return _REGISTRY.get(school_id, {})


def registered_school_ids() -> list[int]:
    """Return every school_id currently registered. Mainly useful for
    diagnostics/tests (e.g. asserting a new school's module loaded)."""
    _load_all()
    return list(_REGISTRY.keys())