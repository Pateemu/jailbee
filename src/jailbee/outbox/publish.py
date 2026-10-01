"""Publish one whole manifest through its existing domain authorization gates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee import issue_outbox, pr, pr_outbox
from jailbee.incus import IncusError
from jailbee.outbox.inspect import build_views, safe_text
from jailbee.outbox.io import PrManagement
from jailbee.outbox.models import (
    ContainerView,
    OutboxChanged,
    OutboxError,
    OutboxExecutionError,
    ProposalId,
    StoreSnapshot,
)
from jailbee.outbox.service import _identity, load_container
from jailbee.outbox_io import (
    ContainerIdentity,
    JournalError,
    JournalStore,
    OutboxReadError,
    journal_key,
)
from jailbee.tui import console, error_plain, info_plain

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


@dataclass(frozen=True)
class PublishOptions:
    dry_run: bool = False
    force: bool = False


def _checked(
    cfg: Config,
    incus: Incus,
    container: str,
    proposal: ProposalId,
    identity: ContainerIdentity,
    journal_store: JournalStore,
    expected_revision: str | None,
) -> tuple[ContainerView, str]:
    fresh = load_container(cfg, incus, container, journal_store=journal_store)
    if not fresh.available or fresh.identity != identity:
        raise OutboxChanged(fresh.error or "container changed; refresh required")
    view = next((v for v in fresh.proposals if v.id == proposal), None)
    if view is None or (expected_revision is not None and view.revision != expected_revision):
        raise OutboxChanged("proposal changed; refresh required")
    if view.error:
        raise OutboxError(f"{proposal}: {view.error}")
    return fresh, view.revision


def issue_outcome_lines(
    batch: issue_outbox.PreparedBatch,
    report: issue_outbox.ApplyReport,
) -> tuple[str, ...]:
    """Format domain receipts, recovery advice and untouched actions without CLI handlers."""
    lines = []
    attempted = set()
    for name, receipt in (*report.applied, *report.skipped):
        attempted.add((name, receipt.index))
        detail = f" ({receipt.url})" if receipt.url else ""
        lines.append(f"{name} action {receipt.index}: applied{detail}")
    failure = report.failure
    if failure is None:
        lines.extend(
            f"{name}: fully applied and removed from the outbox" for name in report.cleaned
        )
    else:
        label = "uncertain" if failure.uncertain else "failed"
        target = failure.manifest or "apply stopped"
        if failure.index is not None:
            target += f" action {failure.index}"
            if failure.manifest is not None:
                attempted.add((failure.manifest, failure.index))
        lines.append(f"{target}: {label} - {failure.detail}")
        if failure.uncertain and failure.manifest is not None and failure.index is not None:
            lines.append(
                f"  resolve: jailbee issue resolve {batch.container} {failure.manifest} "
                f"{failure.index} (--applied --url <url> [--issue <n>] | --retry)"
            )
        lines.extend(
            f"{prepared.manifest.name} action {resolved.index}: pending"
            for prepared in batch.manifests
            for resolved in prepared.actions
            if (prepared.manifest.name, resolved.index) not in attempted
        )
    return tuple(lines)


def _print_lines(lines: list[str] | tuple[str, ...]) -> None:
    for line in lines:
        console.print(safe_text(line), markup=False, highlight=False, soft_wrap=True)


def publish_selected(
    cfg: Config,
    incus: Incus,
    container: str,
    proposal: ProposalId,
    *,
    journal_store: JournalStore,
    options: PublishOptions,
    confirm: Callable[[int], bool],
    expected_revision: str | None = None,
) -> int:
    """Publish all pending actions of one manifest; the callback owns TTY policy.

    The shared UI revision is distinct from a domain proposal digest. Hold the
    same host lock from the first freshness check through domain apply/cleanup;
    the domain's own content, target and recovery gates remain mandatory.
    """
    try:
        if proposal.kind == "issue" and options.force:
            raise OutboxError("force is only valid for PR publication")
        identity = _identity(incus, container)
        manager = PrManagement() if proposal.kind == "pr" else None
        lock = (
            manager.lock(identity)
            if manager is not None
            else journal_store.lock(journal_key(identity, proposal.name))
        )
        with lock:
            fresh, revision = _checked(
                cfg, incus, container, proposal, identity, journal_store, expected_revision
            )
            snapshot = next(s for s in fresh.stores if s.kind == proposal.kind)
            if manager is not None:
                return pr_outbox.offer_pending_comments(
                    cfg,
                    incus,
                    container,
                    container,
                    pr_number=None,
                    confirm=confirm,
                    outbox=pr_outbox.Outbox(snapshot.as_dict(), snapshot.rejected, identity),
                    force=options.force,
                    dry_run=options.dry_run,
                    manifest_names=(proposal.name,),
                    management=manager,
                    expected_revision=revision,
                )

            batch = issue_outbox.prepare_batch(
                cfg,
                incus,
                container,
                (proposal.name,),
                uid=cfg.container_user.uid,
                journal_store=journal_store,
            )
            # Prepare reads independently: do not approve a newer domain proposal
            # merely because its own digest will pass apply_batch's later check.
            prepared_store = StoreSnapshot(
                "issue", tuple(sorted(batch.outbox.files.items())), snapshot.rejected, ()
            )
            prepared = next(
                (
                    v
                    for v in build_views(
                        batch.identity, (prepared_store,), journal_store=journal_store
                    )
                    if v.id == proposal
                ),
                None,
            )
            if batch.identity != identity or prepared is None or prepared.revision != revision:
                raise OutboxChanged("proposal changed during preparation; refresh required")
            _checked(cfg, incus, container, proposal, identity, journal_store, revision)
            _print_lines(issue_outbox.plan_lines(batch))
            if options.dry_run:
                info_plain("Dry run: nothing was published.")
                return 0
            total = sum(a.status == "pending" for m in batch.manifests for a in m.actions)
            if total and not confirm(total):
                info_plain("Nothing published.")
                return 0
            _checked(cfg, incus, container, proposal, identity, journal_store, revision)
            issue_outbox.revalidate_batch(batch)
            _checked(cfg, incus, container, proposal, identity, journal_store, revision)
            report = issue_outbox.apply_batch(
                batch, incus=incus, uid=cfg.container_user.uid, journal_store=journal_store
            )
            _print_lines(issue_outcome_lines(batch, report))
            return int(report.failure is not None)
    except (
        OutboxError,
        OutboxExecutionError,
        JournalError,
        OutboxReadError,
        IncusError,
        issue_outbox.IssueGateError,
        pr.PrError,
    ) as exc:
        error_plain(safe_text(str(exc)))
        return 1
