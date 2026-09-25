"""Message rendering.

A "message" is a small JSON blob (body, button label, optional embed bits).
The same renderer is used by the bot when it sends a DM and by the dashboard
when it draws the live preview, so what you see really is what gets sent.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# Variables the owner can use in any text field.
VARIABLES = (
    "user_name",
    "user_display_name",
    "feeder_name",
    "main_server_name",
    "network_name",
    "invite_url",
)

_TOKEN = re.compile(r"\{([a-zA-Z0-9_]+)\}")

DEFAULT_DM_CONTENT: dict[str, Any] = {
    "body": (
        "Thanks for joining {feeder_name}!\n\n"
        "Most of our main community is over in {main_server_name}. "
        "You're welcome to join us there, or you can ignore this message and stay here."
    ),
    "use_embed": False,
    "embed_title": "",
    "embed_color": "#5865F2",
    "image_url": "",
    "footer": "",
    "button_label": "Join {main_server_name}",
    "button_emoji": "",
    "button_mode": "LINK",
    "button_style": "PRIMARY",
}

DEFAULT_PUBLIC_CONTENT: dict[str, Any] = {
    "body": (
        "Looking for the full community?\n\n"
        "Most of our community is over in {main_server_name}."
    ),
    "use_embed": True,
    "embed_title": "{main_server_name}",
    "embed_color": "#5865F2",
    "image_url": "",
    "footer": "{network_name}",
    "button_label": "Join {main_server_name}",
    "button_emoji": "",
    "button_mode": "LINK",
    "button_style": "PRIMARY",
}

TEXT_FIELDS = ("body", "embed_title", "footer", "button_label", "button_emoji", "image_url")


def default_content(kind: str) -> dict[str, Any]:
    from core import constants

    base = DEFAULT_PUBLIC_CONTENT if kind == constants.KIND_PUBLIC else DEFAULT_DM_CONTENT
    return dict(base)


def build_context(
    *,
    user_name: str = "someone",
    user_display_name: str = "someone",
    feeder_name: str = "this server",
    main_server_name: str = "the main server",
    network_name: str = "our network",
    invite_url: str = "https://discord.gg/",
) -> dict[str, str]:
    return {
        "user_name": user_name,
        "user_display_name": user_display_name,
        "feeder_name": feeder_name,
        "main_server_name": main_server_name,
        "network_name": network_name,
        "invite_url": invite_url,
    }


def render_text(template: str | None, context: dict[str, str]) -> str:
    """Replace {known_variables}. Unknown tokens are left alone rather than
    blowing up, so a typo never stops a DM from going out."""
    if not template:
        return ""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in context:
            return str(context[key])
        return match.group(0)

    return _TOKEN.sub(repl, template)


def render_content(content: dict[str, Any] | None, context: dict[str, str]) -> dict[str, Any]:
    """Render every text field of a message blob, leaving flags untouched."""
    content = dict(content or {})
    out: dict[str, Any] = dict(content)
    for field in TEXT_FIELDS:
        out[field] = render_text(content.get(field, ""), context)
    out["use_embed"] = bool(content.get("use_embed", False))
    out["embed_color"] = content.get("embed_color") or "#5865F2"
    if not out.get("button_label"):
        out["button_label"] = "Join"
    out["button_mode"] = normalise_button_mode(content.get("button_mode"))
    out["button_style"] = normalise_button_style(content.get("button_style"))
    limits={"button_label":80,"embed_title":256,"footer":2048}
    shortened=[]
    for field,limit in limits.items():
        if len(out[field])>limit:
            out[field]=out[field][:limit-1]+"…"
            shortened.append(field)
    body_limit=min(4096,6000-len(out["embed_title"])-len(out["footer"])) if out["use_embed"] else 2000
    if len(out["body"])>body_limit:
        out["body"]=out["body"][:body_limit-1]+"…"
        shortened.append("body")
    out["truncated_fields"]=shortened
    return out


def normalise_button_mode(value: str | None) -> str:
    from core import constants

    value = str(value or "").upper()
    return value if value in constants.BUTTON_MODES else constants.BUTTON_LINK


def normalise_button_style(value: str | None) -> str:
    """Only Discord's real styles. Arbitrary colours are not a thing here."""
    from core import constants

    value = str(value or "").upper()
    return value if value in constants.BUTTON_STYLES else "PRIMARY"


CUSTOM_VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def validate_custom_variables(raw: dict[str, Any] | None) -> tuple[dict[str, str], list[str]]:
    """Clean a set of custom variables, returning (accepted, problems).

    A custom variable may never shadow a built-in: the built-ins carry meaning
    the bot relies on, and silently overriding {invite_url} would break
    attribution.
    """
    accepted: dict[str, str] = {}
    problems: list[str] = []
    for name, value in (raw or {}).items():
        key = str(name).strip().lower()
        if not key:
            continue
        if key in VARIABLES:
            problems.append(f"{{{key}}} is a built-in variable and cannot be redefined")
            continue
        if not CUSTOM_VARIABLE_NAME.match(key):
            problems.append(
                f"{key} is not a valid name (use lower-case letters, digits and underscores)"
            )
            continue
        accepted[key] = str(value)
    return accepted, problems


def unknown_variables(
    content: dict[str, Any] | None, known: Iterable[str] | None = None
) -> list[str]:
    """Variables used in the message that the renderer does not know about."""
    allowed = set(known) if known is not None else set(VARIABLES)
    found: list[str] = []
    for field in TEXT_FIELDS:
        for token in _TOKEN.findall((content or {}).get(field, "") or ""):
            if token not in allowed and token not in found:
                found.append(token)
    return found


# What the Variables panel in Message Studio explains.
VARIABLE_HELP = (
    ("user_name", "newmember", "The member's Discord username"),
    ("user_display_name", "New Member", "Their display name in the feeder"),
    ("feeder_name", "Queue Up", "The feeder server they just joined"),
    ("main_server_name", "Side Quest", "The current main server"),
    ("network_name", "Side Quest Network", "Your network name from Settings"),
    ("invite_url", "https://discord.gg/AAA111", "That feeder's tracking invite"),
)
