"""The moderation port (spec section 4, F7): a prompt is screened before any image spend."""

from typing import Protocol

from gateway.domain.images import Moderation


class Moderator(Protocol):
    @property
    def name(self) -> str: ...

    async def check(self, text: str) -> Moderation:
        """The verdict on the text; ModerationUnavailable when none can be had."""
        ...
