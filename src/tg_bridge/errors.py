"""Errors a Telegram backend raises, sorted by what the bridge does about them."""

from __future__ import annotations


class TransportError(Exception):
    """Telegram could not be reached or did not answer: reconnect with backoff.
    From a send it means the outcome is unknown."""


class AuthError(Exception):
    """The credentials were refused: a configuration error."""


class ToolError(Exception):
    """Telegram (or the tool) answered with an error. From a send it means the
    message was not sent; the text is the reason."""


class ChatAccessError(ToolError):
    """The account may not read or write the chat."""
