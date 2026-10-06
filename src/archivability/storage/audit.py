from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuditContext:
    user_id: str | None = None
    ip_address: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        for field in ("user_id", "ip_address", "session_id"):
            value = getattr(self, field)
            if value is not None and (not value or len(value) > 256):
                raise ValueError(f"{field} must contain between 1 and 256 characters")
