"""Read-only source facts made available to lifecycle hooks for one turn."""

from types import MappingProxyType
from typing import Mapping


_UNKNOWN_TURN_SOURCE_ENVELOPE = MappingProxyType({
    "schema_version": 1,
    "platform": "unknown",
    "human_user_message": "unknown",
    "plain_text_only": "unknown",
    "current_text_isolated": "unknown",
    "forwarded": "unknown",
    "quoted": "unknown",
    "reply_or_reference": "unknown",
})


def unknown_turn_source_envelope() -> Mapping[str, object]:
    """Return the explicit no-ingress-facts envelope for non-gateway turns."""
    return _UNKNOWN_TURN_SOURCE_ENVELOPE
