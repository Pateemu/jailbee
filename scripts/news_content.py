"""Parse and validate the repository's news article sources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Post:
    slug: str
    title: str
    date: date
    summary: str
    image: str | None
    image_alt: str | None
    body_md: str

    @property
    def path(self) -> str:
        """Site-root-relative permalink, WordPress style: /news/YYYY/MM/DD/slug/."""
        return f"/news/{self.date:%Y/%m/%d}/{self.slug}/"


def asset_path(value: str, assets_dir: Path, source: Path) -> Path:
    """Resolve a site-root-relative asset without allowing traversal or symlinks out."""
    if not value.startswith("/assets/"):
        raise ValueError(f"{source}: image must be a local /assets/... path")
    relative = value.removeprefix("/assets/")
    if not relative or any(part in {".", ".."} for part in Path(relative).parts):
        raise ValueError(f"{source}: invalid image path: {value}")
    target = (assets_dir / relative).resolve()
    if not target.is_relative_to(assets_dir.resolve()) or not target.is_file():
        raise ValueError(f"{source}: image does not exist inside assets: {value}")
    return target


def parse_post(path: Path, assets_dir: Path) -> Post:
    match = _FILENAME.fullmatch(path.name)
    if match is None:
        raise ValueError(f"{path}: filename must be YYYY-MM-DD-slug.md")
    filename_date, slug = match.groups()
    if slug == "page":
        raise ValueError(f"{path}: slug 'page' is reserved for archive pagination")
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"{path}: missing YAML front matter (--- delimiters)")
    header, body = text[4:].split("\n---\n", 1)
    try:
        data = yaml.load(header, Loader=yaml.BaseLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML front matter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: front matter must be a mapping")

    def required(key: str) -> str:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}: {key} must be a nonempty string")
        return value.strip()

    title = required("title")
    value = required("date")
    if not _DATE.fullmatch(value):
        raise ValueError(f"{path}: date must be YYYY-MM-DD")
    try:
        published = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{path}: invalid date: {value}") from exc
    if value != filename_date:
        raise ValueError(f"{path}: date must match the filename prefix")
    summary = required("summary")
    image = required("image") if "image" in data else None
    image_alt = required("image_alt") if image is not None else None
    if image is not None:
        asset_path(image, assets_dir, path)
    elif "image_alt" in data:
        raise ValueError(f"{path}: image_alt needs an image")
    return Post(slug, title, published, summary, image, image_alt, body)


def load_posts(posts_dir: Path, assets_dir: Path) -> list[Post]:
    if not posts_dir.exists():
        return []
    posts = [parse_post(source, assets_dir) for source in sorted(posts_dir.glob("*.md"))]
    return sorted(posts, key=lambda post: (-post.date.toordinal(), post.slug))
