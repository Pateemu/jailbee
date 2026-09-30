"""Real proposal inputs shared by outbox service tests."""

import json

from jailbee.outbox.models import Kind, StoreSnapshot
from jailbee.outbox_io import ContainerIdentity

IDENTITY = ContainerIdentity("acme-feature", "2026-09-30T00:00:00Z")


def issue_files() -> dict[str, str]:
    return {
        "001.json": json.dumps(
            {
                "version": 1,
                "actions": [
                    {
                        "type": "create",
                        "repo": ".",
                        "ref": "new",
                        "title": "Example",
                        "body_file": "body.md",
                    },
                    {"type": "comment", "repo": ".", "issue_ref": "new", "body": "Follow-up"},
                    {"type": "comment", "repo": ".", "issue": 42, "body": "Independent"},
                ],
            }
        ),
        "body.md": "Original body",
    }


def pr_files() -> dict[str, str]:
    return {
        "001.json": json.dumps(
            {
                "version": 1,
                "repo": ".",
                "pr": 42,
                "head_sha": "a" * 40,
                "actions": [
                    {
                        "type": "review",
                        "event": "COMMENT",
                        "body": "Review body",
                        "comments": [
                            {"path": "a.py", "line": 1, "body": "First"},
                            {"path": "a.py", "line": 2, "body": "Second"},
                        ],
                    }
                ],
            }
        )
    }


def store(kind: Kind, files: dict[str, str], *, rejected: tuple[str, ...] = ()) -> StoreSnapshot:
    return StoreSnapshot(kind, tuple(sorted(files.items())), rejected, ())
