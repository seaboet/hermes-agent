"""Build the source-only envelope passed from normalized gateway ingress to hooks."""

from types import MappingProxyType
from typing import Mapping

from agent.turn_source import unknown_turn_source_envelope
from gateway.platforms.event import MessageType


def _telegram_raw_fact(event, name: str) -> str:
    raw_message = getattr(event, "raw_message", None)
    if raw_message is None:
        return "unknown"
    return "yes" if getattr(raw_message, name, None) is not None else "no"


def build_turn_source_envelope(event) -> Mapping[str, object]:
    """Freeze authoritative ingress metadata before message text is flattened or enriched."""
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None)
    if platform != "telegram":
        return unknown_turn_source_envelope()

    human_user_message = (
        "no" if getattr(event, "internal", False)
        else "yes" if getattr(source, "user_id", None) and not getattr(source, "is_bot", False)
        else "unknown"
    )
    plain_text_only = (
        "yes" if getattr(event, "message_type", None) is MessageType.TEXT and not getattr(event, "media_urls", None)
        else "no"
    )
    forwarded = _telegram_raw_fact(event, "forward_origin")
    quoted = _telegram_raw_fact(event, "quote")
    reply_or_reference = "yes" if getattr(event, "reply_to_message_id", None) else "no"
    current_text_isolated = (
        "yes" if plain_text_only == "yes" and forwarded == quoted == reply_or_reference == "no"
        else "no" if "yes" in (forwarded, quoted, reply_or_reference) or plain_text_only == "no"
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
