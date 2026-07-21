"""Shared provider exceptions.

Kept in their own tiny module (no imports) so every client — bitbucket.py,
github.py — and the providers facade can subclass/catch these without any
circular-import risk.
"""

from __future__ import annotations


class ProviderError(Exception):
    def __init__(self, who: str, reason: str):
        self.who = who
        self.reason = reason
        super().__init__(f"{who}: {reason}")


class AuthError(ProviderError):
    """Credentials invalid/expired/insufficient — the source can't be read."""
    pass
