"""Sealing with key rotation (22 "Rotating APP_ENCRYPTION_KEY")."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from app.core.security import Sealer

OLD, NEW, OTHER = "o" * 32, "n" * 32, "x" * 32
AD = b"provider-key:w:anthropic"


def test_the_previous_key_still_opens_what_it_sealed_and_new_values_use_the_new_key() -> None:
    sealed_before = Sealer(OLD).seal(b"secret", AD)
    rotating = Sealer(NEW, previous_key=OLD)
    assert rotating.open(sealed_before, AD) == b"secret"
    sealed_now = rotating.seal(b"secret", AD)
    assert Sealer(NEW).open(sealed_now, AD) == b"secret"
    with pytest.raises(InvalidTag):
        Sealer(OLD).open(sealed_now, AD)
    with pytest.raises(InvalidTag):
        Sealer(NEW).open(sealed_before, AD)  # once the previous key is gone, old values are lost


def test_reseal_moves_old_values_to_the_new_key_only() -> None:
    rotating = Sealer(NEW, previous_key=OLD)
    fresh = rotating.reseal(Sealer(OLD).seal(b"secret", AD), AD)
    assert fresh is not None and Sealer(NEW).open(fresh, AD) == b"secret"
    assert rotating.reseal(fresh, AD) is None  # already current
    with pytest.raises(InvalidTag):
        rotating.reseal(Sealer(OTHER).seal(b"secret", AD), AD)
    with pytest.raises(InvalidTag):
        rotating.reseal(Sealer(OLD).seal(b"secret", AD), b"another row")  # still bound to its row
