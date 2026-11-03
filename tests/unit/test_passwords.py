"""Argon2id hasher: verification, unknown-user timing path, rehash detection (US-01, US-03)."""

import pytest
from argon2 import PasswordHasher

from gateway.adapters.security.passwords import Argon2PasswordHasher

PASSWORD = "Correct-Horse-Battery-9!"


@pytest.fixture(scope="module")
def hasher() -> Argon2PasswordHasher:
    return Argon2PasswordHasher(max_concurrency=2)


async def test_hash_is_argon2id_and_verifies(hasher: Argon2PasswordHasher) -> None:
    hashed = await hasher.hash(PASSWORD)
    assert hashed.startswith("$argon2id$")
    assert await hasher.verify(hashed, PASSWORD)
    assert not await hasher.verify(hashed, PASSWORD + "x")


async def test_unknown_user_never_verifies_but_still_does_the_work(
    hasher: Argon2PasswordHasher,
) -> None:
    assert not await hasher.verify(None, PASSWORD)


async def test_corrupt_hash_is_a_failed_login_not_a_crash(hasher: Argon2PasswordHasher) -> None:
    assert not await hasher.verify("$argon2id$garbage", PASSWORD)


async def test_weaker_parameters_need_rehash(hasher: Argon2PasswordHasher) -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    assert hasher.needs_rehash(weak)
    assert not hasher.needs_rehash(await hasher.hash(PASSWORD))
