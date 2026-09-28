"""Editorial input validation for the static news section."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scripts.news_content import load_posts, parse_post


def _post(posts: Path, name: str, metadata: str, body: str = "A story.\n") -> Path:
    posts.mkdir(parents=True, exist_ok=True)
    path = posts / name
    path.write_text(f"---\n{metadata}---\n{body}")
    return path


VALID = 'title: Release\ndate: 2026-09-28\nsummary: "A quoted announcement"\n'


def test_loads_a_post_with_date_metadata_and_a_local_image(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "feature.png").write_bytes(b"image")
    source = _post(
        tmp_path / "posts",
        "2026-09-28-release.md",
        VALID + "image: /assets/feature.png\nimage_alt: Release illustration\n",
    )

    post = parse_post(source, assets)

    assert (post.slug, post.title, post.date, post.summary) == (
        "release",
        "Release",
        date(2026, 9, 28),
        "A quoted announcement",
    )
    assert (post.image, post.image_alt, post.body_md) == (
        "/assets/feature.png",
        "Release illustration",
        "A story.\n",
    )


def test_sorts_latest_first_then_slug_and_accepts_missing_directory(tmp_path: Path) -> None:
    posts = tmp_path / "posts"
    assert load_posts(posts, tmp_path / "assets") == []
    _post(posts, "2026-09-28-zebra.md", VALID)
    _post(posts, "2026-09-28-alpha.md", VALID)
    _post(posts, "2026-09-27-old.md", VALID.replace("2026-09-28", "2026-09-27"))

    assert [p.slug for p in load_posts(posts, tmp_path / "assets")] == [
        "alpha",
        "zebra",
        "old",
    ]


@pytest.mark.parametrize(
    ("name", "metadata", "problem"),
    [
        ("2026-09-28-release.md", "date: 2026-09-28\nsummary: Fine\n", "title"),
        ("2026-09-28-release.md", "title: [Not, scalar]\ndate: 2026-09-28\nsummary: Fine\n", "title"),
        ("2026-09-28-release.md", VALID.replace("2026-09-28", "2026-09-28T12:00:00"), "date"),
        ("2026-09-27-release.md", VALID, "date"),
        ("2026-09-28-Wrong.md", VALID, "filename"),
        ("2026-09-28-page.md", VALID, "page"),
        ("2026-09-28-release.md", VALID + "image: /assets/missing.png\n", "image_alt"),
        (
            "2026-09-28-release.md",
            VALID + "image: /assets/missing.png\nimage_alt: Image\n",
            "image",
        ),
        (
            "2026-09-28-release.md",
            VALID + "image: /assets/../private.png\nimage_alt: Image\n",
            "image",
        ),
    ],
)
def test_rejects_invalid_posts_with_source_path(
    tmp_path: Path, name: str, metadata: str, problem: str
) -> None:
    source = _post(tmp_path / "posts", name, metadata)
    with pytest.raises(ValueError) as error:
        parse_post(source, tmp_path / "assets")
    assert source.name in str(error.value)
    assert problem in str(error.value)


def test_rejects_duplicate_slugs_across_dates(tmp_path: Path) -> None:
    posts = tmp_path / "posts"
    _post(posts, "2026-09-28-release.md", VALID)
    _post(posts, "2026-09-27-release.md", VALID.replace("2026-09-28", "2026-09-27"))

    # Alphabetical source order reads the 27th first; the 28th is the duplicate.
    with pytest.raises(ValueError, match="2026-09-28-release.md"):
        load_posts(posts, tmp_path / "assets")


def test_rejects_feature_image_symlink_outside_asset_directory(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"private")
    (assets / "linked.png").symlink_to(secret)
    source = _post(
        tmp_path / "posts",
        "2026-09-28-release.md",
        VALID + "image: /assets/linked.png\nimage_alt: Image\n",
    )

    with pytest.raises(ValueError, match="image"):
        parse_post(source, assets)
