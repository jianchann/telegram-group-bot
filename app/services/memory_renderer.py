"""Human-readable memory output without internal IDs or provider details."""

from typing import Any

from app.schemas.ai import AssistantResponse
from app.schemas.memory import MemoryEntry


def render_memory_page(
    entries: list[MemoryEntry], total: int, page: int, personal: bool, page_size: int = 20
) -> str:
    command = "/memory_me" if personal else "/memory"
    pages = max(1, (total + page_size - 1) // page_size)
    if page > pages:
        return f"That page does not exist. Use {command} with a page between 1 and {pages}."
    if not entries:
        return (
            "No active memories about you in this group."
            if personal
            else "No active memories in this group yet."
        )
    sections = []
    groups = [entry for entry in entries if entry.scope == "group"]
    people = [entry for entry in entries if entry.scope == "user"]
    if groups:
        sections.append("Group memory\n" + "\n".join(f"• {entry.content}" for entry in groups))
    if people:
        label = (
            "Your memory (shared in this group)"
            if personal
            else "Personal memory (shared in this group)"
        )
        sections.append(
            label
            + "\n"
            + "\n".join(
                f"• {entry.member_name or 'Group member'}: {entry.content}" for entry in people
            )
        )
    footer = f"Page {page}/{pages} · {total} active memories."
    if page < pages:
        footer += f" Next: {command} {page + 1}"
    return "\n\n".join([*sections, footer])


def render_assistant_response(response: AssistantResponse) -> str:
    confirmations = []
    seen: set[tuple[str, str]] = set()
    labels = {
        "saved": "Remembered",
        "updated": "Updated",
        "deleted": "Forgot",
        "unchanged": "Already remembered",
    }
    for item in response.memory_actions:
        action = item.get("action")
        memory: Any = item.get("memory")
        if (
            action not in labels
            or not isinstance(memory, dict)
            or not isinstance(memory.get("content"), str)
        ):
            continue
        key = (str(action), str(memory.get("id", memory["content"])))
        if key in seen:
            continue
        seen.add(key)
        owner = (
            "Group"
            if memory.get("scope") == "group"
            else memory.get("member_name") or "Group member"
        )
        label = (
            "Already forgotten"
            if action == "unchanged" and memory.get("status") == "deleted"
            else labels[action]
        )
        confirmations.append(f"{label} ({owner}): {memory['content']}")
    text = response.text
    if confirmations and response.error_code:
        text = (
            "The memory changes above are confirmed, but I couldn't complete the remaining answer."
        )
    return "\n\n".join([*confirmations, text])
