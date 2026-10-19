"""Render helpers for conversation turns (labels for routine messages)."""


def routine_display_label(user_message: str, max_len: int = 80) -> str:
    """Return a compact label for a scheduled routine turn."""
    if user_message.startswith("[Scheduled routine:"):
        end = user_message.find("]")
        if end != -1:
            label = user_message[len("[Scheduled routine:"):end].strip()
            if label:
                return label[:max_len] + ("..." if len(label) > max_len else "")
    label = user_message.replace("[Scheduled routine] ", "", 1).splitlines()[0].strip()
    return label[:max_len] + ("..." if len(label) > max_len else "")


def routine_full_description(user_message: str) -> str:
    """Strip the [Scheduled routine ...] tag and return the underlying description."""
    if user_message.startswith("[Scheduled routine:"):
        end = user_message.find("]")
        if end != -1:
            return user_message[end + 1:].lstrip()
    return user_message.replace("[Scheduled routine] ", "", 1).strip()


__all__ = ["routine_display_label", "routine_full_description"]
