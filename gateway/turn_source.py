"""Build the source-only envelope passed from normalized gateway ingress to hooks."""

from types import MappingProxyType
from typing import Mapping

from agent.turn_source import unknown_turn_source_envelope
from gateway.platforms.event import MessageType


_UNAVAILABLE = object()


def _descriptor_value(obj, name: str):
    try:
        return getattr(obj, name)
    except Exception:
        return _UNAVAILABLE


def _telegram_raw_fact(raw_message, name: str, *, boolean: bool = False) -> str:
    if raw_message is _UNAVAILABLE or raw_message is None:
        return "unknown"
    value = _descriptor_value(raw_message, name)
    if value is _UNAVAILABLE:
        return "unknown"
    if boolean:
        return "yes" if value else "no"
    return "yes" if value is not None else "no"


def _telegram_event_fact(event, name: str) -> str:
    value = _descriptor_value(event, name)
    if value is _UNAVAILABLE:
        return "unknown"
    return "yes" if value is not None else "no"


def _combined_fact(*facts: str) -> str:
    if "yes" in facts:
        return "yes"
    if "unknown" in facts:
        return "unknown"
    return "no"


def build_turn_source_envelope(event) -> Mapping[str, object]:
    """Freeze authoritative ingress metadata before message text is flattened or enriched."""
    source = _descriptor_value(event, "source")
    if source is _UNAVAILABLE or source is None:
        return unknown_turn_source_envelope()
    source_platform = _descriptor_value(source, "platform")
    platform = (
        _descriptor_value(source_platform, "value")
        if source_platform is not _UNAVAILABLE and source_platform is not None
        else _UNAVAILABLE
    )
    if platform is _UNAVAILABLE or not platform:
        return unknown_turn_source_envelope()
    if platform != "telegram":
        return MappingProxyType({**unknown_turn_source_envelope(), "platform": platform})

    internal = _descriptor_value(event, "internal")
    is_bot = _descriptor_value(source, "is_bot")
    user_id = _descriptor_value(source, "user_id")
    human_user_message = (
        "unknown" if internal is _UNAVAILABLE
        else "no" if internal
        else "unknown" if is_bot is _UNAVAILABLE
        else "no" if is_bot
        else "unknown" if user_id is _UNAVAILABLE or not user_id
        else "yes"
    )
    message_type = _descriptor_value(event, "message_type")
    media_urls = _descriptor_value(event, "media_urls")
    plain_text_only = (
        "unknown" if message_type is _UNAVAILABLE or media_urls is _UNAVAILABLE or message_type is None or media_urls is None
        else "yes" if message_type is MessageType.TEXT and not media_urls
        else "no"
    )
    raw_message = _descriptor_value(event, "raw_message")
    forwarded = _combined_fact(
        _telegram_raw_fact(raw_message, "forward_origin"),
        _telegram_raw_fact(raw_message, "is_automatic_forward", boolean=True),
    )
    quoted = _telegram_raw_fact(raw_message, "quote")
    reply_or_reference = _combined_fact(
        _telegram_event_fact(event, "reply_to_message_id"),
        _telegram_raw_fact(raw_message, "reply_to_message"),
        _telegram_raw_fact(raw_message, "external_reply"),
        _telegram_raw_fact(raw_message, "reply_to_story"),
        _telegram_raw_fact(raw_message, "reply_to_checklist_task_id"),
        _telegram_raw_fact(raw_message, "reply_to_poll_option_id"),
        _telegram_raw_fact(raw_message, "pinned_message"),
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
