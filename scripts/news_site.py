"""Build the branded, static news section from repository Markdown."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, time
from email.utils import format_datetime
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
_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


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


def _archive_url(page: int) -> str:
    return "/news/" if page == 1 else f"/news/page/{page}/"


def _feed(posts: list[Post], path: Path) -> None:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    for tag, value in (
        ("title", "JailBee News"),
        ("link", f"{URL}/news/"),
        ("description", "JailBee releases and announcements."),
    ):
        ET.SubElement(channel, tag).text = value
    for post in posts:
        item = ET.SubElement(channel, "item")
        canonical = f"{URL}/news/{post.slug}/"
        for tag, value in (
            ("title", post.title),
            ("link", canonical),
            ("guid", canonical),
            ("description", post.summary),
            ("pubDate", format_datetime(datetime.combine(post.date, time.min, UTC), usegmt=True)),
        ):
            ET.SubElement(item, tag).text = value
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


def _sitemap(website_dir: Path, site_dir: Path, urls: list[str]) -> None:
    ET.register_namespace("", _SITEMAP_NS)
    tree = ET.parse(website_dir / "sitemap.xml")
    root = tree.getroot()
    for url in urls:
        entry = ET.SubElement(root, f"{{{_SITEMAP_NS}}}url")
        ET.SubElement(entry, f"{{{_SITEMAP_NS}}}loc").text = f"{URL}{url}"
    tree.write(site_dir / "sitemap.xml", encoding="utf-8", xml_declaration=True)


def build(website_dir: Path, site_dir: Path) -> None:
    posts = load_posts(website_dir / "news" / "posts", website_dir / "assets")
    env = Environment(
        loader=FileSystemLoader(website_dir / "news" / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    output = site_dir / "news"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    shared = {
        "style_hash": _hash(website_dir / "assets" / "style.css"),
        "news_hash": _hash(website_dir / "assets" / "news.css"),
    }
    archives = [posts[start : start + 10] for start in range(0, len(posts), 10)] or [[]]
    sitemap_urls = []
    for number, page_posts in enumerate(archives, 1):
        url = _archive_url(number)
        sitemap_urls.append(url)
        target = output if number == 1 else output / "page" / str(number)
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text(
            env.get_template("index.html").render(
                posts=page_posts,
                previous=_archive_url(number - 1) if number > 1 else None,
                next=_archive_url(number + 1) if number < len(archives) else None,
                canonical=f"{URL}{url}",
                title="News — JailBee" if number == 1 else f"News, page {number} — JailBee",
                **shared,
            ),
            encoding="utf-8",
        )
    for post in posts:
        sitemap_urls.append(f"/news/{post.slug}/")
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
    _feed(posts, output / "feed.xml")
    _sitemap(website_dir, site_dir, sitemap_urls)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--website-dir", type=Path, default=WEBSITE)
    parser.add_argument("--site-dir", type=Path, default=SITE)
    args = parser.parse_args(argv)
    try:
        build(args.website_dir, args.site_dir)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
