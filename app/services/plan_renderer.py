"""Compact Telegram rendering for plans without exposing internal identifiers."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from app.schemas.ai import AssistantResponse
from app.services.memory_renderer import render_assistant_response as render_memory_response
from app.services.source_renderer import render_research_sources


def _value(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _date(value: Any) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value) if value else None


def _dates(plan: Any) -> str | None:
    start, end = _date(_value(plan, "start_date")), _date(_value(plan, "end_date"))
    if start and end:
        return start if start == end else f"{start} to {end}"
    return start or end


def render_plan_page(plans: Iterable[Any], total: int, page: int, page_size: int = 20) -> str:
    pages = max(1, (total + page_size - 1) // page_size)
    plans = list(plans)
    if page > pages:
        return f"That page does not exist. Use /plans with a page between 1 and {pages}."
    if not plans:
        return "There are no active plans in this group yet."
    lines = ["Active plans"]
    for plan in plans:
        name = str(_value(plan, "name", "Untitled plan"))
        details = [str(value) for value in (_value(plan, "plan_type"), _dates(plan)) if value]
        lines.append(f"• {name}" + (f" — {' · '.join(details)}" if details else ""))
    lines.extend(("", f"Page {page}/{pages} · {total} active plans."))
    if page < pages:
        lines[-1] += f" Next: /plans {page + 1}"
    return "\n".join(lines)


def render_plan_candidates(candidates: Iterable[Any]) -> str:
    names = [str(_value(candidate, "name", candidate)) for candidate in candidates]
    if not names:
        return "I couldn't find an active plan with that name. Use /plans to see active plans."
    return "Which active plan did you mean?\n" + "\n".join(f"• {name}" for name in names)


def render_task_page(
    tasks: Iterable[Any], total: int, page: int, mine: bool, page_size: int = 20
) -> str:
    command = "/tasks mine" if mine else "/tasks"
    pages = max(1, (total + page_size - 1) // page_size)
    tasks = list(tasks)
    if page > pages:
        return f"That page does not exist. Use {command} with a page between 1 and {pages}."
    if not tasks:
        return (
            "You have no outstanding assigned tasks." if mine else "There are no outstanding tasks."
        )
    lines = ["My outstanding tasks" if mine else "Outstanding tasks"]
    for task in tasks:
        details = [str(_value(task, "status", "open")).replace("_", " ")]
        if assigned := _value(task, "assigned_name"):
            details.append(str(assigned))
        if due := _date(_value(task, "due_at")):
            details.append(due)
        lines.append(
            f"• [{_value(task, 'plan_name', 'Untitled plan')}] "
            f"{_value(task, 'title', 'Untitled task')} — {' · '.join(details)}"
        )
    footer = f"Page {page}/{pages} · {total} outstanding task{'s' if total != 1 else ''}."
    if page < pages:
        footer += f" Next: {command} {page + 1}"
    lines.extend(("", footer))
    return "\n".join(lines)


def render_plan_detail(plan: Any, item_limit: int = 50) -> str:
    lines = [str(_value(plan, "name", "Untitled plan"))]
    details = [
        str(value)
        for value in (_value(plan, "plan_type"), _value(plan, "status"), _dates(plan))
        if value
    ]
    if details:
        lines.append(" · ".join(details))
    if description := _value(plan, "description"):
        lines.extend(("", str(description)))

    members = [
        member
        for member in (_value(plan, "members", []) or [])
        if _value(member, "status", "active") == "active"
    ]
    if members:
        lines.extend(("", "Participants"))
        for member in members:
            name = _value(member, "display_name") or _value(member, "name") or "Group member"
            role = _value(member, "role")
            lines.append(f"• {name}" + (f" — {role}" if role else ""))

    items = list(_value(plan, "items", []) or [])
    shown = items[:item_limit]
    groups: dict[str, list[Any]] = defaultdict(list)
    for item in shown:
        groups[str(_value(item, "item_type", "note"))].append(item)
    labels = {
        "decision": "Decisions",
        "constraint": "Constraints",
        "task": "Tasks",
        "activity": "Activities",
        "open_question": "Open questions",
        "note": "Notes",
    }
    order = ("decision", "constraint", "task", "activity", "open_question", "note")
    for kind in (*order, *(key for key in groups if key not in order)):
        if not (entries := groups.get(kind)):
            continue
        lines.extend(("", labels.get(kind, kind.replace("_", " ").title())))
        for item in entries:
            title = str(_value(item, "title", "Untitled item"))
            assigned = _value(item, "assigned_name") or _value(item, "assigned_member_name")
            metadata = [
                str(value)
                for value in (_value(item, "status"), assigned, _date(_value(item, "due_at")))
                if value
            ]
            lines.append(f"• {title}" + (f" — {' · '.join(metadata)}" if metadata else ""))
            if kind == "decision":
                item_metadata = _value(item, "metadata", {}) or {}
                selected = _value(item_metadata, "selected")
                reason = _value(item_metadata, "reason")
                alternatives = _value(item_metadata, "alternatives")
                if selected:
                    lines.append(f"  Selected: {selected}")
                if reason:
                    lines.append(f"  Reason: {reason}")
                if isinstance(alternatives, list) and alternatives:
                    lines.append(
                        "  Alternatives: " + ", ".join(str(value) for value in alternatives)
                    )
    artifacts = list(_value(plan, "artifacts", []) or [])
    if artifacts:
        artifact_groups: dict[str, list[Any]] = defaultdict(list)
        for artifact in artifacts:
            artifact_groups[str(_value(artifact, "category") or "Links")].append(artifact)
        lines.extend(("", "Saved links"))
        for category, entries in artifact_groups.items():
            if category != "Links":
                lines.append(category)
            for artifact in entries:
                url = str(_value(artifact, "url", ""))
                title = _value(artifact, "title")
                lines.append(f"• {title} — {url}" if title else f"• {url}")
    total_items = _value(plan, "total_item_count", len(items))
    omitted = max(0, int(total_items) - len(shown))
    if omitted:
        lines.extend(("", f"{omitted} more item{'s' if omitted != 1 else ''} omitted."))
    if not items:
        lines.extend(("", "No plan items yet."))
    total_artifacts = int(_value(plan, "total_artifact_count", len(artifacts)) or 0)
    artifact_omitted = max(0, total_artifacts - len(artifacts))
    if artifact_omitted:
        lines.extend(("", f"{artifact_omitted} more saved link(s) omitted."))
    return "\n".join(lines)


def render_assistant_response(response: AssistantResponse) -> str:
    """Add deterministic confirmations for committed plan mutations."""
    sourced_text = render_research_sources(
        response.text,
        getattr(response, "research_sources", ()),
        getattr(response, "url_statuses", ()),
    )
    base = render_memory_response(
        AssistantResponse(
            text=sourced_text,
            error_code=response.error_code,
            memory_actions=getattr(response, "memory_actions", []),
        )
    )
    actions = getattr(response, "plan_actions", None)
    if actions is None:
        actions = [
            outcome
            for outcome in getattr(response, "state_actions", [])
            if str(_value(outcome, "kind", "")).startswith("plan")
        ]
    labels = {
        ("plan", "created"): "Created plan",
        ("plan", "updated"): "Updated plan",
        ("plan", "unchanged"): "Plan already set",
        ("plan_item", "created"): "Added",
        ("plan_item", "added"): "Added",
        ("plan_item", "updated"): "Updated",
        ("plan_item", "deleted"): "Removed",
        ("plan_item", "removed"): "Removed",
        ("plan_item", "unchanged"): "Already set",
        ("plan_member", "created"): "Updated participant",
        ("plan_member", "updated"): "Updated participant",
        ("plan_member", "deleted"): "Removed participant",
        ("plan_member", "unchanged"): "Participant already set",
        ("plan_artifact", "created"): "Saved link",
        ("plan_artifact", "updated"): "Updated saved link",
        ("plan_artifact", "unchanged"): "Link already saved",
    }
    confirmations: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for outcome in actions or []:
        action = str(_value(outcome, "action", ""))
        entity = _value(outcome, "entity", {})
        title = (
            _value(entity, "name")
            or _value(entity, "title")
            or _value(entity, "display_name")
            or _value(entity, "url")
        )
        kind = str(_value(outcome, "kind", "plan"))
        key = (kind, action, str(title))
        label = labels.get((kind, action))
        if kind == "plan" and action == "updated":
            lifecycle = {
                "completed": "Completed plan",
                "cancelled": "Cancelled plan",
                "archived": "Archived plan",
            }
            label = lifecycle.get(str(_value(entity, "status", "")), label)
        if label is None or not isinstance(title, str) or not title or key in seen:
            continue
        seen.add(key)
        confirmations.append(f"{label}: {title}")
    if not confirmations:
        confirmations = []
    for outcome in getattr(response, "telegram_actions", []):
        if _value(outcome, "kind") != "telegram_poll" or _value(outcome, "action") != "created":
            continue
        question = _value(_value(outcome, "entity", {}), "question")
        if isinstance(question, str) and question:
            confirmations.append(f"Created poll: {question}")
    if not confirmations:
        return base
    if response.error_code:
        memory_confirmations = render_memory_response(
            AssistantResponse(text="", memory_actions=getattr(response, "memory_actions", []))
        ).strip()
        failure = "The changes above are confirmed, but I couldn't complete the remaining answer."
        return "\n\n".join(
            [*(value for value in (memory_confirmations, *confirmations) if value), failure]
        )
    return "\n\n".join(value for value in (*confirmations, base) if value)
