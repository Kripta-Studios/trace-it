"""Provision a private deployment account without passing passwords in process arguments.

Run: python -m app.features.users.password EMAIL --name NAME --role manager
Use --password-stdin for provisioning through a protected pipe or secret file.
Changing a password revokes all existing browser sessions for that account.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.core.api_auth import API_USER_EMAIL
from app.core.database import session_factory
from app.features.users.model import User
from app.features.users.security import hash_password


async def provision(email: str, password: str, name: str | None, role: str | None) -> None:
    if email == API_USER_EMAIL:
        raise ValueError("The reserved API manager cannot be used as a browser account")
    encoded = hash_password(password)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            if not name or not role:
                raise ValueError("New accounts require --name and --role")
            user = User(email=email, name=name, role=role, session_version=0)
            session.add(user)
        else:
            if name is not None:
                user.name = name
            if role is not None:
                user.role = role
            user.session_version += 1
        user.password_hash = encoded
        await session.commit()
    print("Account provisioned; previous browser sessions have been revoked.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("--name")
    parser.add_argument("--role", choices=("manager", "operator"))
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass()
    if not args.password_stdin and password != getpass.getpass("Confirm password: "):
        parser.error("Passwords do not match")
    try:
        asyncio.run(provision(args.email, password, args.name, args.role))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
