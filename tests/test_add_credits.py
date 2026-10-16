"""scripts/add_credits.py, run like the README says."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

REPO = Path(__file__).resolve().parent.parent


def add_credits(server, *args):
    return subprocess.run(
        [sys.executable, "scripts/add_credits.py", *args],
        cwd=REPO, env={**os.environ, "NANOSAAS_CONFIG": str(server.config)},
        capture_output=True, text=True, timeout=60,
    )


async def test_it_adds_credits_to_the_account_of_an_email(server, account):
    email = f"{account.google_id}@example.com"
    r = add_credits(server, email, "25")
    assert (r.returncode, r.stdout) == (0, f"{email} has 30 credits now.\n")
    assert await account.credits() == 30


async def test_it_says_when_there_is_no_such_account(server, account):
    r = add_credits(server, "nobody@example.com", "25")
    assert (r.returncode, r.stderr) == (1, "There's no account with the email nobody@example.com.\n")


@pytest.mark.parametrize("credits", ["0", "-5", "many"])
async def test_it_only_adds_one_credit_or_more(server, account, credits):
    r = add_credits(server, f"{account.google_id}@example.com", credits)
    assert r.returncode == 2
    assert await account.credits() == 5
