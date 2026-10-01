"""Fold the pre-`pr:` spelling of the PR-generation settings into `pr`.

Until the `pr:` block existed the five settings lived on the Claude agent
(`agents.claude.ai_pr_*` and `pr_prompt`, or the same under the legacy
top-level `claude:` block). They describe `jailbee pr`, not Claude, so they
moved. The old spelling is still read — and still warned about — until
`LEGACY_REMOVAL_VERSION`.

Pure on purpose, like `resolve_browsers_raw`: the caller that knows which file
a key came from emits the notice, and every caller that merely wants the
normalised mapping (the config editor, `tests.conftest.make_config`) stays
quiet by construction.
"""

from __future__ import annotations

# old key on the Claude block -> its name inside `pr:`
LEGACY_PR_KEYS: dict[str, str] = {
    "ai_pr_description": "ai_description",
    "ai_pr_branch": "ai_branch",
    "ai_pr_model": "model",
    "ai_pr_timeout": "timeout",
    "pr_prompt": "prompt",
}


def _split_legacy(block: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """`(block without the legacy keys, the legacy keys renamed for `pr`)`."""
    kept = {k: v for k, v in block.items() if k not in LEGACY_PR_KEYS}
    moved = {LEGACY_PR_KEYS[k]: v for k, v in block.items() if k in LEGACY_PR_KEYS}
    return kept, moved


def fold_legacy_pr_keys(raw: dict[str, object]) -> tuple[dict[str, object], bool]:
    """Move `ai_pr_*` / `pr_prompt` out of the Claude block into `pr`.

    Returns `(folded, changed)` and never mutates `raw`. A key already present
    in `pr` wins over its legacy spelling: the new block is the one the user
    wrote on purpose. A `pr` that is not a mapping is left for validation to
    reject, and the legacy keys stay where they are so that error is not
    joined by a second, misleading one about them.
    """
    pr = raw.get("pr", {})
    if not isinstance(pr, dict):
        return raw, False

    out = dict(raw)
    moved: dict[str, object] = {}

    legacy_block = out.get("claude")
    if isinstance(legacy_block, dict):
        out["claude"], found = _split_legacy(legacy_block)
        moved.update(found)

    agents = out.get("agents")
    if isinstance(agents, dict) and isinstance(agents.get("claude"), dict):
        kept, found = _split_legacy(agents["claude"])
        out["agents"] = {**agents, "claude": kept}
        moved.update(found)

    if not moved:
        return raw, False
    out["pr"] = {**moved, **pr}
    return out, True
