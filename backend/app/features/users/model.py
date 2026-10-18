from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, created_at

ROLES = ("manager", "operator")


class User(Base):
    """A person using the app. A `manager` resolves escalations and approves rules."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint(f"role in {ROLES}", name="role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str] = mapped_column(String, unique=True)
    role: Mapped[str] = mapped_column(default="operator")
    password_hash: Mapped[str | None]
    session_version: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[created_at]
