"""Build the source-only envelope passed from normalized gateway ingress to hooks."""

from types import MappingProxyType
from typing import Mapping

from agent.turn_source import unknown_turn_source_envelope
from gateway.platforms.event import MessageType


def _telegram_raw_fact(event, name: str) -> str:
    raw_message = getattr(event, "raw_message", None)
    if raw_message is None or not hasattr(raw_message, name):
        return "unknown"
    return "yes" if getattr(raw_message, name) is not None else "no"


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
        else "unknown" if not hasattr(source, "user_id") or not hasattr(source, "is_bot")
        else "yes" if source.user_id and not source.is_bot
        else "unknown"
    )
    message_type = getattr(event, "message_type", None)
    media_urls = getattr(event, "media_urls", None)
    plain_text_only = (
        "unknown" if message_type is None or media_urls is None
        else "yes" if message_type is MessageType.TEXT and not media_urls
        else "no"
    )
    forwarded = _telegram_raw_fact(event, "forward_origin")
    quoted = _telegram_raw_fact(event, "quote")
    reply_or_reference = (
        "unknown" if not hasattr(event, "reply_to_message_id")
        else "yes" if event.reply_to_message_id else "no"
    )
    current_text_isolated = (
        "yes" if human_user_message == plain_text_only == "yes" and forwarded == quoted == reply_or_reference == "no"
        else "no" if "no" in (human_user_message, plain_text_only) or "yes" in (forwarded, quoted, reply_or_reference)
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
