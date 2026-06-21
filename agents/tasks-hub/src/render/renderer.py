"""Jinja2 renderer for view results (idea 12).

Templates live alongside the view specs at src/render/templates/*.j2.
The renderer is intentionally thin: it takes a view-run result dict
and a template name, returns plaintext markdown. Carry-over and the
3-3-3 plan structure are expressed in the templates so they can be
adjusted without changing Python.

Templates have access to:
  view              the view result dict from view.run_view()
  sections          shortcut to view['sections']
  generated_at      ISO timestamp
  today             today's local date (YYYY-MM-DD)
  fmt_tags(tags)    "@phone @laptop" string
  fmt_effort(min)   "~15m" / "~2h" / "~deep"
  fmt_due(iso)      "12.07" or "12.07.2026" if not this year
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def fmt_tags(tags: list[str] | None) -> str:
    if not tags:
        return ""
    return " ".join(tags)


def fmt_effort(effort_min: Optional[int]) -> str:
    if effort_min is None:
        return ""
    if effort_min >= 90:
        return "~deep"
    if effort_min >= 60 and effort_min % 60 == 0:
        return f"~{effort_min // 60}h"
    return f"~{effort_min}m"


def fmt_due(iso: Optional[str], *, today: Optional[date] = None) -> str:
    if not iso:
        return ""
    base = today or date.today()
    try:
        y, m, d = iso.split("-")
        if int(y) == base.year:
            return f"{d}.{m}"
        return f"{d}.{m}.{y}"
    except ValueError:
        return iso


def build_env(*, templates_dir: Optional[Path] = None) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir or _TEMPLATES_DIR)),
        autoescape=select_autoescape(disabled_extensions=(".j2",), default=False),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["fmt_tags"] = fmt_tags
    env.filters["fmt_effort"] = fmt_effort
    env.filters["fmt_due"] = fmt_due
    return env


def render(
    view_result: dict[str, Any],
    *,
    template: str,
    templates_dir: Optional[Path] = None,
    today: Optional[date] = None,
) -> str:
    env = build_env(templates_dir=templates_dir)
    tpl = env.get_template(template)
    return tpl.render(
        view=view_result,
        sections=view_result.get("sections") or [],
        generated_at=view_result.get("generated_at"),
        today=(today or date.today()).isoformat(),
    )
