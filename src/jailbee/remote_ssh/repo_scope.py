"""Immutable repository visibility policy for an SSH session."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.remote_ssh.session import SSH_EXCLUDED_REPOS_ENV, is_ssh_session

if TYPE_CHECKING:
    from collections.abc import Mapping

_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RepoScopeError(ValueError):
    """An SSH child has no trustworthy repository policy snapshot."""


@dataclass(frozen=True)
class RemoteRepoScope:
    excluded: frozenset[str]

    def allows(self, prefix: str | None) -> bool:
        return prefix is None or prefix not in self.excluded


def scope_for_session(environ: Mapping[str, str] | None = None) -> RemoteRepoScope:
    """Read the server-created snapshot, requiring one for every SSH child."""
    env = os.environ if environ is None else environ
    if not is_ssh_session(env):
        return RemoteRepoScope(frozenset())
    raw = env.get(SSH_EXCLUDED_REPOS_ENV)
    try:
        values = json.loads(raw) if raw is not None else None
    except (json.JSONDecodeError, TypeError) as exc:
        raise RepoScopeError("Invalid SSH repository policy snapshot") from exc
    if (
        not isinstance(values, list)
        or any(not isinstance(value, str) or not _PREFIX_RE.fullmatch(value) for value in values)
        or len(values) != len(set(values))
    ):
        raise RepoScopeError("Invalid SSH repository policy snapshot")
    return RemoteRepoScope(frozenset(values))
