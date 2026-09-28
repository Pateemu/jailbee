"""Read-only resolution of the host branch used for status comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jailbee import git

TargetSource = Literal["local", "tracking", "unavailable"]
TrackingRelation = Literal[
    "equal", "local-ahead", "tracking-ahead", "diverged", "unavailable"
]


@dataclass(frozen=True)
class TargetSnapshot:
    """Resolved host target ref and its relation to the fetched upstream ref."""

    branch: str
    sha: str | None
    source: TargetSource
    upstream_ref: str
    tracking_relation: TrackingRelation


def resolve_target(repo_root: Path, branch: str, upstream_remote: str) -> TargetSnapshot:
    """Resolve a target from the local branch, falling back to its tracking ref.

    The current checkout is deliberately irrelevant: this reads named refs only
    and never contacts the remote or changes repository state.
    """
    local_sha = git.rev_parse(repo_root, f"refs/heads/{branch}")
    upstream_ref = f"refs/remotes/{upstream_remote}/{branch}"
    tracking_sha = git.rev_parse(repo_root, upstream_ref)

    if local_sha is None:
        return TargetSnapshot(
            branch=branch,
            sha=tracking_sha,
            source="tracking" if tracking_sha is not None else "unavailable",
            upstream_ref=upstream_ref,
            tracking_relation="unavailable",
        )

    if tracking_sha is None:
        relation: TrackingRelation = "unavailable"
    elif local_sha == tracking_sha:
        relation = "equal"
    else:
        local_is_ancestor = git.is_ancestor(repo_root, local_sha, tracking_sha)
        tracking_is_ancestor = git.is_ancestor(repo_root, tracking_sha, local_sha)
        if local_is_ancestor is None or tracking_is_ancestor is None:
            relation = "unavailable"
        elif local_is_ancestor:
            relation = "tracking-ahead"
        elif tracking_is_ancestor:
            relation = "local-ahead"
        else:
            relation = "diverged"

    return TargetSnapshot(
        branch=branch,
        sha=local_sha,
        source="local",
        upstream_ref=upstream_ref,
        tracking_relation=relation,
    )
