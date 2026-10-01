"""Bounded, read-only snapshots of container-written outbox stores."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import io
import os
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import local
from typing import TYPE_CHECKING, cast

from jailbee.db import state_dir
from jailbee.incus import IncusError
from jailbee.outbox.models import Kind, OutboxChanged, OutboxExecutionError, StoreSnapshot
from jailbee.outbox_io import ContainerIdentity

if TYPE_CHECKING:
    from jailbee.incus import Incus

FILE_LIMIT = 256 * 1024
SNAPSHOT_LIMIT = 8 * 1024 * 1024
# Inspection is interactive: a wedged daemon/helper must fail, not hang or look empty.
READ_TIMEOUT = 30
_METADATA = ".jailbee-snapshot"

# Copy into a private staging directory before archiving. Never follow links or
# block on a raced FIFO; recheck identity/link count after copying. Metadata is a
# versioned NUL-delimited record, so arbitrary rejected names need no JSON tools.
_READ_SCRIPT = r"""
set -euo pipefail
export LC_ALL=C
store=$1
limit=$2
cap=$3
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
mkdir "$tmp/files"
meta="$tmp/files/.jailbee-snapshot"
check_path() {
    local path=/ component
    IFS=/ read -ra components <<< "${store#/}"
    for component in "${components[@]}"; do
        [[ -x "$path" && -d "$path" && ! -L "$path" ]] || {
            echo 'inaccessible ancestor' >&2; exit 1;
        }
        path="${path%/}/$component"
        [[ ! -L "$path" ]] || { echo 'symlink ancestor' >&2; exit 1; }
        if [[ ! -e "$path" ]]; then
            [[ -r "${path%/*}" ]] || { echo 'inaccessible ancestor' >&2; exit 1; }
            return 2
        fi
        [[ -d "$path" ]] || { echo 'non-directory ancestor' >&2; exit 1; }
    done
    [[ -r "$store" && -x "$store" ]] || { echo 'inaccessible store' >&2; exit 1; }
}
if check_path; then
    printf 'v1\0present\0' > "$meta"
    cd -- "$store"
    store_identity=$(stat -c '%d:%i' .)
    shopt -s nullglob dotglob
    total=0
    for file in ./*; do
        name=${file##*/}
        reject=false
        if [[ "$name" == .jailbee-snapshot || -L "$file" || ! -f "$file" ]]; then
            reject=true
        else
            before=$(stat -c '%d:%i:%h:%s:%y:%z' -- "$file")
            links=$(stat -c %h -- "$file")
            size=$(stat -c %s -- "$file")
            if (( links != 1 || size > limit )); then
                reject=true
            else
                # GNU dd bounds even a concurrently growing file and refuses symlinks.
                dd if="$file" of="$tmp/files/$name" iflag=nofollow,nonblock \
                    bs=262145 count=1 status=none
                after=$(stat -c '%d:%i:%h:%s:%y:%z' -- "$file")
                copied=$(stat -c %s -- "$tmp/files/$name")
                if [[ "$before" != "$after" ]] || (( copied != size )); then
                    echo 'store changed while reading' >&2; exit 1
                fi
                total=$((total + ((copied + 511) / 512 + 1) * 512))
                (( total * 4 / 3 <= cap )) || { echo 'snapshot overflow' >&2; exit 1; }
            fi
        fi
        if "$reject"; then
            printf 'R\0%s\0' "$name" >> "$meta"
        fi
        (( $(stat -c %s -- "$meta") <= limit )) || { echo 'metadata overflow' >&2; exit 1; }
    done
    check_path || { echo 'store changed while reading' >&2; exit 1; }
    [[ "$(stat -c '%d:%i' -- "$store")" == "$store_identity" ]] || {
        echo 'store replaced' >&2; exit 1;
    }
else
    printf 'v1\0missing\0' > "$meta"
fi
# The extra byte detects overflow rather than silently accepting truncation.
# pipefail preserves tar failures, including SIGPIPE when the bound is reached.
if ! tar -C "$tmp/files" --format=ustar -cf - -- . | base64 -w0 |
    head -c "$((cap + 1))" > "$tmp/encoded"; then
    echo 'snapshot overflow or archive failure' >&2; exit 1
fi
(( $(stat -c %s "$tmp/encoded") <= cap )) || { echo 'snapshot overflow' >&2; exit 1; }
cat "$tmp/encoded"
"""


def store_directory(kind: Kind) -> str:
    """Return the existing store location; inspection never creates it."""
    if kind not in ("pr", "issue"):
        raise OutboxExecutionError("unknown outbox kind")
    return f"/home/dev/.jailbee/{kind}-outbox"


def _decode(kind: Kind, raw: str) -> StoreSnapshot:
    if len(raw) > SNAPSHOT_LIMIT:
        raise OutboxExecutionError("snapshot overflow")
    try:
        blob = base64.b64decode(raw, validate=True)
        files: dict[str, str] = {}
        rejected: list[str] = []
        warnings: list[str] = []
        metadata: bytes | None = None
        seen: set[str] = set()
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
            for member in archive:
                if member.name == "." and member.isdir():
                    continue
                name = member.name.removeprefix("./")
                if not name or "/" in name or name in seen:
                    raise OutboxExecutionError("invalid snapshot archive entry")
                seen.add(name)
                if name == _METADATA:
                    if not member.isfile() or member.size > FILE_LIMIT:
                        raise OutboxExecutionError("invalid snapshot metadata")
                elif not member.isfile() or member.size > FILE_LIMIT:
                    rejected.append(name)
                    warnings.append(f"Rejected unsafe or oversized file: {name}")
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    raise OutboxExecutionError("unreadable snapshot entry")
                content = stream.read(FILE_LIMIT + 1)
                if len(content) != member.size:
                    raise OutboxExecutionError("truncated snapshot entry")
                if name == _METADATA:
                    metadata = content
                else:
                    try:
                        files[name] = content.decode("utf-8")
                    except UnicodeDecodeError:
                        rejected.append(name)
                        warnings.append(f"Rejected non-UTF8 file: {name}")
        if metadata is None:
            raise OutboxExecutionError("missing snapshot metadata")
        fields = metadata.decode("utf-8").split("\0")
        if fields[:2] not in (["v1", "present"], ["v1", "missing"]) or fields[-1] != "":
            raise OutboxExecutionError("invalid snapshot metadata")
        records = fields[2:-1]
        if len(records) % 2 or (fields[1] == "missing" and (records or files or rejected)):
            raise OutboxExecutionError("invalid snapshot metadata")
        for tag, value in zip(records[::2], records[1::2], strict=True):
            if tag not in ("R", "W") or not value:
                raise OutboxExecutionError("invalid snapshot metadata record")
            if tag == "R":
                if "/" in value or value in files or value in rejected:
                    raise OutboxExecutionError("invalid rejected inventory")
                rejected.append(value)
            else:
                warnings.append(value)
        return StoreSnapshot(kind, tuple(sorted(files.items())), tuple(rejected), tuple(warnings))
    except (ValueError, UnicodeError, binascii.Error, tarfile.TarError, OSError) as exc:
        raise OutboxExecutionError(f"invalid snapshot: {exc}") from exc


def read_store(incus: Incus, container: str, kind: Kind, *, uid: int | None) -> StoreSnapshot:
    """Read a strict bounded snapshot, raising on unavailable or malformed stores."""
    directory = store_directory(kind)
    try:
        raw = incus.exec(
            container,
            ["bash", "-c", _READ_SCRIPT, "bash", directory, str(FILE_LIMIT), str(SNAPSHOT_LIMIT)],
            uid=uid,
            timeout=READ_TIMEOUT,
        )
    except IncusError as exc:
        raise OutboxExecutionError(f"outbox unavailable: {exc}") from exc
    return _decode(kind, raw)


class PrManagement:
    """Instance-reentrant, host-side serialization of one container's PR store."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else state_dir() / "pr-outbox" / "locks"
        self._lock_state = local()

    @property
    def identity(self) -> ContainerIdentity | None:
        """The identity owned by this thread's outermost operation, if any."""
        return cast("ContainerIdentity | None", getattr(self._lock_state, "identity", None))

    @contextmanager
    def lock(self, identity: ContainerIdentity) -> Iterator[None]:
        if not identity.full_name or not identity.created_at:
            raise OutboxExecutionError("cannot lock an empty container identity")
        if self.identity is not None and self.identity != identity:
            raise OutboxChanged("container changed during publication; refresh required")
        digest = hashlib.sha256(f"{identity.full_name}\0{identity.created_at}".encode()).hexdigest()
        path = self.root / f"{digest}.lock"
        held = getattr(self._lock_state, "paths", None)
        if held is None:
            held = set()
            self._lock_state.paths = held
        if path in held:
            yield
            return
        descriptor = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.chmod(path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise OutboxExecutionError("could not lock PR outbox") from exc
        held.add(path)
        self._lock_state.identity = identity
        try:
            yield
        finally:
            held.remove(path)
            self._lock_state.identity = None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
