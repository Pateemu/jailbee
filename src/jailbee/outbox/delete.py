"""Pure raw-JSON deletion plans; execution must recheck progress and neighbors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jailbee import issue_manifest as issues
from jailbee import pr_outbox as prs
from jailbee.outbox.models import ContainerView, Kind, OutboxError, ProposalId, StoreSnapshot


@dataclass(frozen=True)
class DeleteSelection:
    action: int | None = None
    comment: int | None = None
    with_dependents: bool = False
    archive_journal: bool = False


@dataclass(frozen=True)
class DeletePlan:
    proposal: ProposalId
    expected_revision: str
    selection: DeleteSelection
    removed_actions: tuple[int, ...]
    removed_comments: tuple[tuple[int, int], ...]
    new_text: str | None
    delete_names: tuple[str, ...]
    summary: tuple[str, ...]


def _parse(kind: Kind, name: str, text: str, files: dict[str, str]) -> dict[str, Any]:
    try:
        if kind == "issue":
            issues.parse_manifest(name, text, files)
        else:
            prs.parse_manifest(name, text, files)
        raw: dict[str, Any] = json.loads(text)
        return raw
    except (issues.IssueManifestError, prs.ManifestError, ValueError, RecursionError) as exc:
        raise OutboxError(str(exc)) from exc


def _body_names(kind: Kind, raw: dict[str, Any]) -> set[str]:
    items = list(raw["actions"])
    if kind == "pr":
        items.extend(c for a in raw["actions"] if a["type"] == "review" for c in a["comments"])
    return {item["body_file"] for item in items if isinstance(item.get("body_file"), str)}


def _whole_names(store: StoreSnapshot, name: str, raw: dict[str, Any] | None) -> tuple[str, ...]:
    # Rejected entries may conceal references, including non-addressable manifests.
    if raw is None or store.rejected:
        return (name,)
    files = store.as_dict()
    referenced: set[str] = set()
    for other, text in files.items():
        if other == name or not other.endswith(".json") or other.endswith(".progress.json"):
            continue
        try:
            referenced.update(_body_names(store.kind, _parse(store.kind, other, text, files)))
        except OutboxError:
            return (name,)
    return (name, *sorted(_body_names(store.kind, raw) - referenced))


def _index(value: int, count: int, label: str) -> None:
    if type(value) is not int or not 0 <= value < count:
        raise OutboxError(f"{label} must be a zero-based index in 0..{count - 1}")


def plan_delete(
    container: ContainerView, proposal: ProposalId, selection: DeleteSelection
) -> DeletePlan:
    """Plan one deletion against immutable inspection inputs without any I/O.

    ``delete_names`` is only a conservative candidate scope, not authorization:
    the executor must re-read neighbor inventory as well as the selected revision.
    """
    if not container.available or container.identity is None:
        raise OutboxError("container is unavailable")
    view = next((v for v in container.proposals if v.id == proposal), None)
    store = next((s for s in container.stores if s.kind == proposal.kind), None)
    if view is None or store is None:
        raise OutboxError(f"{proposal}: proposal is not in the inspected outbox")
    whole = selection.action is None and selection.comment is None
    if selection.comment is not None and selection.action is None:
        raise OutboxError("a comment selector requires an action selector")
    if selection.archive_journal and (proposal.kind != "issue" or not whole):
        raise OutboxError("archive_journal is only valid for whole issue deletion")
    if selection.with_dependents and (
        proposal.kind != "issue" or whole or selection.comment is not None
    ):
        raise OutboxError("with_dependents requires an issue-create action deletion")
    if view.edit_block:
        if not (
            selection.archive_journal
            and view.error is None
            and view.state in ("partial", "applied")
            and all(a.state != "uncertain" for a in view.actions)
        ):
            raise OutboxError(view.edit_block)

    raw = None
    if proposal.name not in store.rejected:
        try:
            raw = _parse(proposal.kind, proposal.name, view.raw_text, store.as_dict())
        except OutboxError:
            if not whole:
                raise
    if not whole and raw is None:
        raise OutboxError("invalid manifest cannot be selectively edited")
    removed_actions: tuple[int, ...] = ()
    removed_comments: tuple[tuple[int, int], ...] = ()
    new_text = None
    if raw is not None:
        actions = raw["actions"]
        if whole:
            removed_actions = tuple(range(len(actions)))
        else:
            assert selection.action is not None
            index = selection.action
            _index(index, len(actions), "action")
            action = actions[index]
            updated = dict(raw)
            surviving = list(actions)
            if selection.comment is not None:
                if proposal.kind != "pr" or action["type"] != "review":
                    raise OutboxError("a comment selector requires a PR review action")
                comment = selection.comment
                _index(comment, len(action["comments"]), "comment")
                surviving[index] = dict(action)
                surviving[index]["comments"] = [
                    c for i, c in enumerate(action["comments"]) if i != comment
                ]
                removed_comments = ((index, comment),)
            else:
                is_create = proposal.kind == "issue" and action["type"] == "create"
                if selection.with_dependents and not is_create:
                    raise OutboxError("with_dependents requires an issue-create action deletion")
                dependents = tuple(
                    i
                    for i, a in enumerate(actions)
                    if is_create and a.get("issue_ref") == action["ref"]
                )
                if dependents and not selection.with_dependents:
                    raise OutboxError(
                        f"action {index} has dependent actions {dependents}; use with_dependents"
                    )
                removed_actions = tuple(sorted((index, *dependents)))
                surviving = [a for i, a in enumerate(actions) if i not in removed_actions]
            updated["actions"] = surviving
            if surviving:
                new_text = json.dumps(updated, ensure_ascii=True, indent=2) + "\n"
                _parse(proposal.kind, proposal.name, new_text, store.as_dict())
    delete_names = _whole_names(store, proposal.name, raw) if new_text is None else ()
    summary = (
        f"Delete {proposal}" if new_text is None else f"Edit {proposal}",
        f"Removed actions: {removed_actions}; removed comments: {removed_comments}",
        f"Delete files: {delete_names}",
    )
    return DeletePlan(
        proposal,
        view.revision,
        selection,
        removed_actions,
        removed_comments,
        new_text,
        delete_names,
        summary,
    )
