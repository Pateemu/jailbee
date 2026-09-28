"""Build the branded, static news section from repository Markdown."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape

from scripts.news_content import Post, asset_path, load_posts

REPO = Path(__file__).resolve().parents[1]
WEBSITE = REPO / "website"
SITE = REPO / "_site"
URL = "https://jailbee.gisgro.io"
DEFAULT_IMAGE = "/assets/img/jailbee-og.png"
_IMAGE = re.compile(r"!\[[^]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_HTML = re.compile(r"</?[a-zA-Z][^>]*>|<!--|<!")
_H1 = re.compile(r"(?m)^\s{0,3}#(?:\s|$)")


def _content(post: Post, assets_dir: Path) -> str:
    source = Path(f"{post.date}-{post.slug}.md")
    if _HTML.search(post.body_md):
        raise ValueError(f"{source}: raw HTML is not allowed in article Markdown")
    if _H1.search(post.body_md):
        raise ValueError(f"{source}: use ## for article headings, not a second # title")
    for path in _IMAGE.findall(post.body_md):
        asset_path(path, assets_dir, source)
    return markdown.markdown(post.body_md, extensions=["fenced_code", "tables"])


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def build(website_dir: Path, site_dir: Path) -> None:
    posts = load_posts(website_dir / "news" / "posts", website_dir / "assets")
    env = Environment(
        loader=FileSystemLoader(website_dir / "news" / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    output = site_dir / "news"
    output.mkdir(parents=True, exist_ok=True)
    shared = {
        "style_hash": _hash(website_dir / "assets" / "style.css"),
        "news_hash": _hash(website_dir / "assets" / "news.css"),
    }
    (output / "index.html").write_text(
        env.get_template("index.html").render(
            posts=posts, canonical=f"{URL}/news/", title="News — JailBee", **shared
        ),
        encoding="utf-8",
    )
    for post in posts:
        target = output / post.slug
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text(
            env.get_template("article.html").render(
                post=post,
                body_html=_content(post, website_dir / "assets"),
                canonical=f"{URL}/news/{post.slug}/",
                title=f"{post.title} — JailBee",
                og_image=f"{URL}{post.image or DEFAULT_IMAGE}",
                **shared,
            ),
            encoding="utf-8",
        )
