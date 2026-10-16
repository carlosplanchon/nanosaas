#!/usr/bin/env python3
"""
Add credits to a user's account, found by the email the user logs in
with. Run it from the repository root, where config.toml is, or in the
api container:

    docker compose exec api python scripts/add_credits.py user@example.com 25
"""
import argparse
import asyncio
import sys
from pathlib import Path

# The app's modules are in the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tortoise import Tortoise
from tortoise.expressions import F

from shared import TORTOISE_ORM
from web.db_models import Account


async def add_credits(email: str, credits: int) -> int | None:
    """Add the credits, and return the new balance, or None if there's no such account."""
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        # In a single UPDATE, like Account.reserve_credit(), so it can't undo a task's.
        if not await Account.filter(email=email).update(credits=F("credits") + credits):
            return None
        return (await Account.get(email=email)).credits
    finally:
        await Tortoise.close_connections()


def main():
    parser = argparse.ArgumentParser(description="Add credits to a user's account.")
    parser.add_argument("email", help="the email the user logs in with")
    parser.add_argument("credits", type=int, help="how many credits to add")
    args = parser.parse_args()
    if args.credits < 1:
        parser.error("credits must be 1 or more")

    balance = asyncio.run(add_credits(args.email, args.credits))
    if balance is None:
        raise SystemExit(f"There's no account with the email {args.email}.")
    print(f"{args.email} has {balance} credits now.")


if __name__ == "__main__":
    main()
