"""SFTP and SCP over a virtual tree of containers' repo directories.

``/`` lists the containers this service may show; ``/<container>/…`` is that
container's repository directory. Nothing here reads or writes a host path: the
base ``asyncssh.SFTPServer`` implements every operation on the *host*
filesystem (and looks users up in the host's passwd), so this subclass
overrides every method that is not a pure helper — a test keeps it that way —
and sends the real work to ``ContainerFS``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

import asyncssh
from asyncssh import (
    FXF_APPEND,
    FXF_CREAT,
    FXF_EXCL,
    FXF_TRUNC,
    FXF_WRITE,
    SFTPAttrs,
    SFTPFailure,
    SFTPName,
    SFTPNoSuchFile,
    SFTPOpUnsupported,
    SFTPPermissionDenied,
    SFTPServer,
)

from jailbee.remote_ssh.container_fs import ContainerFS, FileStat, FSError

if TYPE_CHECKING:
    from jailbee.incus import Incus
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

log = logging.getLogger(__name__)

T = TypeVar("T")

# Largest file one upload may grow to, and the largest single read served.
MAX_FILE_BYTES = 2 * 1024**3
MAX_READ_BYTES = 1024 * 1024
# How long a container listing is trusted before `incus list` is asked again.
_CATALOG_TTL_SECONDS = 5.0
# Concurrent `incus exec` calls this service lets through, across every session.
MAX_CONCURRENT_EXECS = 8


def split_path(path: bytes) -> tuple[str, ...]:
    """A client path as components: `.`/empty dropped, `..` clamped at the root.

    An absolute path means "from the root of the tree". NUL and non-UTF-8 bytes
    are refused: names travel to the container as UTF-8 argv.
    """
    try:
        text = path.decode("utf-8")
    except UnicodeDecodeError:
        raise SFTPFailure("file names must be UTF-8") from None
    if "\0" in text:
        raise SFTPFailure("invalid file name")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return tuple(parts)


def _virtual_dir_attrs() -> SFTPAttrs:
    now = int(time.time())
    return SFTPAttrs(size=0, uid=0, gid=0, permissions=0o040555, atime=now, mtime=now)


def _attrs(st: FileStat) -> SFTPAttrs:
    return SFTPAttrs(
        size=st.size, uid=st.uid, gid=st.gid, permissions=st.mode, atime=st.atime, mtime=st.mtime
    )


def _sftp_error(exc: FSError) -> asyncssh.SFTPError:
    message = str(exc)
    if exc.kind == "not_found":
        return SFTPNoSuchFile(message)
    if exc.kind == "denied":
        return SFTPPermissionDenied(message)
    return SFTPFailure(message)


@dataclass(frozen=True)
class ContainerRef:
    name: str
    repo: str
    repo_dir: str


@dataclass
class SFTPService:
    """What every SFTP session of one server run shares."""

    incus: Incus
    scope: RemoteRepoScope
    gate: asyncio.Semaphore

    def server(self, chan: asyncssh.SSHServerChannel[bytes]) -> JailbeeSFTPServer:
        return JailbeeSFTPServer(chan, self)


class _Catalog:
    """Which containers a session may see, and each one's `ContainerFS` (blocking calls)."""

    def __init__(self, incus: Incus, scope: RemoteRepoScope) -> None:
        self._incus = incus
        self._scope = scope
        self._listed_at = float("-inf")
        self._refs: dict[str, ContainerRef] = {}
        self._fs: dict[str, ContainerFS] = {}

    def containers(self) -> dict[str, ContainerRef]:
        if time.monotonic() - self._listed_at < _CATALOG_TTL_SECONDS:
            return self._refs
        refs: dict[str, ContainerRef] = {}
        for raw in self._incus.list_containers(fast=True):
            repo = next(
                (
                    p[: -len("-base")]
                    for p in raw.get("profiles") or []
                    if p.endswith("-base") and p != "default"
                ),
                None,
            )
            repo_dir = (raw.get("config") or {}).get("user.jailbee.repo_dir")
            if (
                repo is None
                or not isinstance(repo_dir, str)
                or not repo_dir
                or raw.get("status") != "Running"
                or not self._scope.allows(repo)
            ):
                continue
            refs[raw["name"]] = ContainerRef(raw["name"], repo, repo_dir)
        self._refs, self._listed_at = refs, time.monotonic()
        return refs

    def fs(self, ref: ContainerRef) -> ContainerFS:
        cached = self._fs.get(ref.name)
        if cached is not None:
            return cached
        probe = self._incus.exec_bytes(
            ref.name, ["stat", "-c", "%u %g", "--", ref.repo_dir], timeout=30
        )
        try:
            uid, gid = (int(x) for x in probe.stdout.split())
        except ValueError:
            raise FSError("not_found", "repository directory not found") from None
        if probe.returncode != 0:
            raise FSError("not_found", "repository directory not found")
        if uid == 0:
            # Files must not be created as root; the clone is the container user's.
            raise FSError("denied", "repository directory is owned by root")
        fs = ContainerFS(self._incus, ref.name, ref.repo_dir, uid, gid)
        self._fs[ref.name] = fs
        return fs


@dataclass(frozen=True)
class _Handle:
    parts: tuple[str, ...]
    writable: bool
    append: bool


class JailbeeSFTPServer(SFTPServer):
    def __init__(self, chan: asyncssh.SSHServerChannel[bytes], service: SFTPService) -> None:
        super().__init__(chan)
        self._service = service
        self._catalog = _Catalog(service.incus, service.scope)

    # ---- plumbing -----------------------------------------------------------

    async def _blocking(self, fn: Callable[..., T], *args: Any) -> T:
        async with self._service.gate:
            return await asyncio.to_thread(fn, *args)

    def _audit(self, op: str, container: str | None, vpath: str, result: str) -> None:
        log.info(
            "SFTP source=%r fingerprint=%s container=%s op=%s path=%r result=%s",
            self.channel.get_extra_info("peername"),
            self.channel.get_extra_info("jailbee_key_fingerprint"),
            container,
            op,
            vpath,
            result,
        )

    async def _on_container(
        self, op: str, parts: tuple[str, ...], call: Callable[[ContainerFS, str], T]
    ) -> T:
        vpath = "/" + "/".join(parts)
        container = parts[0] if parts else None
        result = "ok"
        try:
            ref = (await self._blocking(self._catalog.containers)).get(parts[0]) if parts else None
            if ref is None:
                raise FSError("not_found", "no such file or directory")
            fs = await self._blocking(self._catalog.fs, ref)
            return await self._blocking(call, fs, "/".join(parts[1:]))
        except FSError as exc:
            result = exc.kind
            raise _sftp_error(exc) from None
        except asyncssh.SFTPError:
            result = "refused"
            raise
        except Exception:
            result = "error"
            log.exception("SFTP internal error op=%s container=%s", op, container)
            raise SFTPFailure("internal error") from None
        finally:
            self._audit(op, container, vpath, result)

    @staticmethod
    def _need_inside_repo(parts: tuple[str, ...]) -> None:
        if len(parts) < 2:
            raise SFTPPermissionDenied("the top of the tree is read-only")

    # ---- metadata -----------------------------------------------------------

    async def stat(self, path: bytes) -> SFTPAttrs:
        parts = split_path(path)
        if not parts:
            return _virtual_dir_attrs()
        return _attrs(
            await self._on_container("stat", parts, lambda fs, rel: fs.stat(rel, follow=True))
        )

    async def lstat(self, path: bytes) -> SFTPAttrs:
        parts = split_path(path)
        if not parts:
            return _virtual_dir_attrs()
        return _attrs(
            await self._on_container("lstat", parts, lambda fs, rel: fs.stat(rel, follow=False))
        )

    async def fstat(self, file_obj: object) -> SFTPAttrs:
        handle = _as_handle(file_obj)
        return _attrs(
            await self._on_container(
                "fstat", handle.parts, lambda fs, rel: fs.stat(rel, follow=True)
            )
        )

    async def scandir(self, path: bytes) -> AsyncIterator[SFTPName]:
        parts = split_path(path)
        for dots in (b".", b".."):
            yield SFTPName(dots, attrs=_virtual_dir_attrs())
        if not parts:
            refs = await self._blocking(self._catalog.containers)
            for name in sorted(refs):
                yield SFTPName(name.encode(), attrs=_virtual_dir_attrs())
            return
        entries = await self._on_container("scandir", parts, lambda fs, rel: fs.listdir(rel))
        for entry in entries:
            yield SFTPName(entry.name.encode(), attrs=_attrs(entry.stat))

    def realpath(self, path: bytes) -> bytes:
        return ("/" + "/".join(split_path(path))).encode()

    async def readlink(self, path: bytes) -> bytes:
        parts = split_path(path)
        self._need_inside_repo(parts)
        target = await self._on_container("readlink", parts, lambda fs, rel: fs.readlink(rel))
        return target.encode()

    async def setstat(self, path: bytes, attrs: SFTPAttrs) -> None:
        await self._setstat("setstat", split_path(path), attrs)

    async def fsetstat(self, file_obj: object, attrs: SFTPAttrs) -> None:
        await self._setstat("fsetstat", _as_handle(file_obj).parts, attrs)

    async def _setstat(self, op: str, parts: tuple[str, ...], attrs: SFTPAttrs) -> None:
        self._need_inside_repo(parts)
        mode = attrs.permissions & 0o7777 if attrs.permissions is not None else None
        size, atime, mtime = attrs.size, attrs.atime, attrs.mtime
        if mode is None and size is None and atime is None and mtime is None:
            return  # ownership and the rest are ignored, never an error
        await self._on_container(
            op,
            parts,
            lambda fs, rel: fs.setstat(
                rel,
                mode=mode,
                size=size,
                atime=int(atime) if atime is not None else None,
                mtime=int(mtime) if mtime is not None else None,
            ),
        )

    # ---- file content -------------------------------------------------------

    async def open(self, path: bytes, pflags: int, attrs: SFTPAttrs) -> object:
        parts = split_path(path)
        self._need_inside_repo(parts)
        create, truncate, exclusive = (
            bool(pflags & FXF_CREAT),
            bool(pflags & FXF_TRUNC),
            bool(pflags & FXF_EXCL),
        )
        writable = bool(pflags & (FXF_WRITE | FXF_APPEND))
        if (create or truncate or exclusive) and not writable:
            raise SFTPPermissionDenied("not opened for writing")
        await self._on_container(
            "open",
            parts,
            lambda fs, rel: fs.open(rel, create=create, truncate=truncate, exclusive=exclusive),
        )
        return _Handle(parts, writable, bool(pflags & FXF_APPEND))

    async def read(self, file_obj: object, offset: int, size: int) -> bytes:
        handle = _as_handle(file_obj)
        wanted = min(size, MAX_READ_BYTES)
        return await self._on_container(
            "read", handle.parts, lambda fs, rel: fs.read(rel, offset, wanted)
        )

    async def write(self, file_obj: object, offset: int, data: bytes) -> int:
        handle = _as_handle(file_obj)
        if not handle.writable:
            raise SFTPPermissionDenied("not opened for writing")
        if handle.append:
            offset = (
                await self._on_container(
                    "write", handle.parts, lambda fs, rel: fs.stat(rel, follow=True)
                )
            ).size
        if offset + len(data) > MAX_FILE_BYTES:
            raise SFTPFailure("file too large")
        await self._on_container("write", handle.parts, lambda fs, rel: fs.write(rel, offset, data))
        return len(data)

    def close(self, file_obj: object) -> None:
        return None  # handles hold no container state

    def fsync(self, file_obj: object) -> None:
        return None

    # ---- namespace changes --------------------------------------------------

    async def mkdir(self, path: bytes, attrs: SFTPAttrs) -> None:
        parts = split_path(path)
        self._need_inside_repo(parts)
        mode = attrs.permissions if attrs.permissions is not None else 0o755
        await self._on_container("mkdir", parts, lambda fs, rel: fs.mkdir(rel, mode & 0o777))

    async def remove(self, path: bytes) -> None:
        parts = split_path(path)
        self._need_inside_repo(parts)
        await self._on_container("remove", parts, lambda fs, rel: fs.remove(rel))

    async def rmdir(self, path: bytes) -> None:
        parts = split_path(path)
        self._need_inside_repo(parts)
        await self._on_container("rmdir", parts, lambda fs, rel: fs.rmdir(rel))

    async def rename(self, oldpath: bytes, newpath: bytes) -> None:
        await self._rename("rename", oldpath, newpath, overwrite=False)

    async def posix_rename(self, oldpath: bytes, newpath: bytes) -> None:
        await self._rename("posix_rename", oldpath, newpath, overwrite=True)

    async def _rename(self, op: str, oldpath: bytes, newpath: bytes, *, overwrite: bool) -> None:
        old, new = split_path(oldpath), split_path(newpath)
        self._need_inside_repo(old)
        self._need_inside_repo(new)
        if old[0] != new[0]:
            raise SFTPPermissionDenied("cannot move files between containers")
        new_rel = "/".join(new[1:])
        await self._on_container(
            op, old, lambda fs, rel: fs.rename(rel, new_rel, overwrite=overwrite)
        )

    # ---- refused or unsupported ----------------------------------------------

    def symlink(self, oldpath: bytes, newpath: bytes) -> None:
        raise SFTPPermissionDenied("links cannot be created")

    def link(self, oldpath: bytes, newpath: bytes) -> None:
        raise SFTPPermissionDenied("links cannot be created")

    def lsetstat(self, path: bytes, attrs: SFTPAttrs) -> None:
        raise SFTPPermissionDenied("links cannot be modified")

    def statvfs(self, path: bytes) -> Any:
        raise SFTPOpUnsupported("statvfs is not supported")

    def fstatvfs(self, file_obj: object) -> Any:
        raise SFTPOpUnsupported("statvfs is not supported")

    def lock(self, file_obj: object, offset: int, length: int, flags: int) -> None:
        raise SFTPOpUnsupported("byte range locks are not supported")

    def unlock(self, file_obj: object, offset: int, length: int) -> None:
        raise SFTPOpUnsupported("byte range locks are not supported")

    def open56(self, path: bytes, desired_access: int, flags: int, attrs: SFTPAttrs) -> Any:
        raise SFTPOpUnsupported("only SFTP version 3 is supported")

    # ---- host-facing helpers the base class would otherwise use ---------------

    def map_path(self, path: bytes) -> bytes:
        raise SFTPFailure("host paths are not available")

    def reverse_map_path(self, path: bytes) -> bytes:
        raise SFTPFailure("host paths are not available")

    def format_user(self, uid: int | None) -> str:
        return "" if uid is None else str(uid)  # never the host's passwd

    def format_group(self, gid: int | None) -> str:
        return "" if gid is None else str(gid)

    def exit(self) -> None:
        return None


def _as_handle(file_obj: object) -> _Handle:
    if not isinstance(file_obj, _Handle):
        raise SFTPFailure("invalid handle")
    return file_obj
