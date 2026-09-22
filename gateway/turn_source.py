"""Build the source-only envelope passed from normalized gateway ingress to hooks."""

from types import MappingProxyType
from typing import Mapping

from agent.turn_source import unknown_turn_source_envelope
from gateway.platforms.event import MessageType


def _telegram_raw_fact(event, name: str, *, boolean: bool = False) -> str:
    raw_message = getattr(event, "raw_message", None)
    if raw_message is None:
        return "unknown"
    try:
        value = getattr(raw_message, name)
    except Exception:
        return "unknown"
    if boolean:
        return "yes" if value else "no"
    return "yes" if value is not None else "no"


def _telegram_event_fact(event, name: str) -> str:
    if not hasattr(event, name):
        return "unknown"
    return "yes" if getattr(event, name) is not None else "no"


def _combined_fact(*facts: str) -> str:
    if "yes" in facts:
        return "yes"
    if "unknown" in facts:
        return "unknown"
    return "no"


def build_turn_source_envelope(event) -> Mapping[str, object]:
    """Freeze authoritative ingress metadata before message text is flattened or enriched."""
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None)
    if not platform:
        return unknown_turn_source_envelope()
    if platform != "telegram":
        return MappingProxyType({**unknown_turn_source_envelope(), "platform": platform})

    human_user_message = (
        "unknown" if not hasattr(event, "internal") or source is None
        else "no" if event.internal
        else "unknown" if not hasattr(source, "is_bot")
        else "no" if source.is_bot
        else "unknown" if not hasattr(source, "user_id") or not source.user_id
        else "yes"
    )
    message_type = getattr(event, "message_type", None)
    media_urls = getattr(event, "media_urls", None)
    plain_text_only = (
        "unknown" if message_type is None or media_urls is None
        else "yes" if message_type is MessageType.TEXT and not media_urls
        else "no"
    )
    forwarded = _combined_fact(
        _telegram_raw_fact(event, "forward_origin"),
        _telegram_raw_fact(event, "is_automatic_forward", boolean=True),
    )
    quoted = _telegram_raw_fact(event, "quote")
    reply_or_reference = _combined_fact(
        _telegram_event_fact(event, "reply_to_message_id"),
        _telegram_raw_fact(event, "reply_to_message"),
        _telegram_raw_fact(event, "external_reply"),
        _telegram_raw_fact(event, "reply_to_story"),
        _telegram_raw_fact(event, "reply_to_checklist_task_id"),
        _telegram_raw_fact(event, "reply_to_poll_option_id"),
        _telegram_raw_fact(event, "pinned_message"),
    )
    current_text_isolated = (
        "yes" if plain_text_only == "yes" and forwarded == quoted == reply_or_reference == "no"
        else "no" if plain_text_only == "no" or "yes" in (forwarded, quoted, reply_or_reference)
        else "unknown"
    )
    return MappingProxyType({
        "schema_version": 1,
        "platform": "telegram",
        "human_user_message": human_user_message,
        "plain_text_only": plain_text_only,
        "current_text_isolated": current_text_isolated,
        "forwarded": forwarded,
        "quoted": quoted,
        "reply_or_reference": reply_or_reference,
    })
