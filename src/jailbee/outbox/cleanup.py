"""Pure body cleanup scope; store metadata is never disposable body content."""

from collections.abc import Iterable


def exclusive_body_names(
    candidates: Iterable[str], present: Iterable[str], referenced: Iterable[str]
) -> tuple[str, ...]:
    """Keep manifests, progress and receipt history even when named as bodies."""
    return tuple(
        sorted(
            name
            for name in set(candidates) & set(present) - set(referenced)
            if name != "applied.log"
            and not name.endswith(".json")
            and name not in ("", ".", "..")
            and not any(c in name for c in ("/", "\\", "\0"))
        )
    )
