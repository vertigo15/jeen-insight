"""Simple user resolver for Jeen Insights."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class User:
    """User information."""
    id: str
    name: str
    email: Optional[str] = None


class SimpleUserResolver:
    """
    Resolve the active user from ``user_context`` on each query request.

    The Flask UI forwards ``user_id`` (and optional name/email) from the signed
    session cookie; direct API callers without context fall back to ``default``.
    """
    
    def __init__(self, default_user_id: str = "default"):
        self.default_user = User(
            id=default_user_id,
            name="Default User",
            email="user@example.com"
        )
    
    async def resolve_user(self, context: Optional[dict] = None) -> User:
        """Resolve user from context."""
        if context and "user_id" in context:
            return User(
                id=context["user_id"],
                name=context.get("user_name", "User"),
                email=context.get("user_email")
            )
        return self.default_user
    
    def get_default_user(self) -> User:
        """Get default user."""
        return self.default_user
