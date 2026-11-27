from __future__ import annotations

import re
import unittest

from app.domain.account_types import ACCOUNT_TYPES
from app.tests.support import PROJECT_ROOT


PROFILE_TABLE = PROJECT_ROOT / "proxy/src/core/upstream_profile.h"

# `{"<sub-type>", <needs_session>, CooldownRecoveryChannel::<channel>, ...}`
PROFILE_ROW = re.compile(
    r'\{\s*"([^"]+)"\s*,\s*(true|false)\s*,\s*'
    r"CooldownRecoveryChannel::(\w+)")


def _declared_profiles() -> dict[str, tuple[bool, str]]:
    """The sub-type rows of `upstream_profile_for_subtype()`.

    The header keeps the table as a plain initializer list precisely so it can
    be read without a C++ toolchain; the default profile lives outside it and
    is not enumerated here.
    """
    source = PROFILE_TABLE.read_text(encoding="utf-8")
    start = source.index("profiles[] = {")
    end = source.index("};", start)
    rows = PROFILE_ROW.findall(source[start:end])
    return {name: (needs_session == "true", channel)
            for name, needs_session, channel in rows}


def _declared_subtypes() -> dict[str, object]:
    return {subtype: sub
            for type_spec in ACCOUNT_TYPES.values()
            for subtype, sub in type_spec.subtypes.items()}


class AccountTypeParityTest(unittest.TestCase):
    """The Python spec and the C++ profile table must describe one behaviour.

    `AccountSubtypeSpec.session_header` and `usage_path` are the declarative
    half; the proxy dispatches on `needs_session` and `cooldown_recovery`.  The
    documentation requires them to agree, so this pins the pair instead of
    leaving the drift to a review.
    """

    def test_every_declared_subtype_has_a_proxy_profile(self) -> None:
        profiles = _declared_profiles()
        for subtype in _declared_subtypes():
            assert subtype in profiles, (
                f"{subtype} is declared in ACCOUNT_TYPES but missing from "
                f"{PROFILE_TABLE.name}")

    def test_no_proxy_profile_is_undeclared(self) -> None:
        declared = _declared_subtypes()
        for subtype in _declared_profiles():
            assert subtype in declared, (
                f"{subtype} has a proxy profile but no AccountSubtypeSpec")

    def test_session_header_matches_needs_session(self) -> None:
        profiles = _declared_profiles()
        for subtype, spec in _declared_subtypes().items():
            needs_session, _ = profiles[subtype]
            assert needs_session == spec.session_header, (
                f"{subtype}: session_header={spec.session_header} but the "
                f"proxy profile sets needs_session={needs_session}")

    def test_usage_path_selects_the_usage_snapshot_channel(self) -> None:
        profiles = _declared_profiles()
        for subtype, spec in _declared_subtypes().items():
            _, channel = profiles[subtype]
            expects_snapshot = bool(spec.usage_path)
            assert (channel == "kUsageSnapshot") == expects_snapshot, (
                f"{subtype}: usage_path={spec.usage_path!r} but the proxy "
                f"profile recovers through {channel}")
