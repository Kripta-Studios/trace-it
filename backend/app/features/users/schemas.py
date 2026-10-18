from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class UserIn(BaseModel):
    name: str
    email: str
    role: Literal["manager", "operator"] = "operator"


class UserOut(UserIn):
    id: int


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: SecretStr | None = Field(default=None, max_length=1024)


class CreateUserIn(UserIn):
    password: SecretStr | None = Field(default=None, min_length=12, max_length=1024)
