"""Provider contract for the trusted, local notification sender."""
from typing import Protocol


class RateLimited(Exception):
    """The provider explicitly declined the request. A retry is safe."""


class Rejected(Exception):
    """The provider rejected the request. Fix its configuration before retrying."""


class Uncertain(Exception):
    """The provider might have accepted the request."""


class NotificationProvider(Protocol):
    def send(self, recipient: str, run: str, status: str, links: list[str]) -> str:
        """Submit the summary and return its provider receipt."""
        ...

