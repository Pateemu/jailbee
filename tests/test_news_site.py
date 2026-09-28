"""Generated news pages and syndication, built without a server or network."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.news_site import build

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def site(tmp_path: Path) -> tuple[Path, Path]:
    website = tmp_path / "website"
    output = tmp_path / "_site"
    templates = website / "news" / "templates"
    templates.parent.mkdir(parents=True)
    shutil.copytree(REPO / "website" / "news" / "templates", templates)
    assets = website / "assets"
    assets.mkdir()
    (assets / "style.css").write_text("body { color: red; }")
    (assets / "news.css").write_text("article { max-width: 60ch; }")
    (assets / "feature.png").write_bytes(b"image")
    return website, output


def _add(website: Path, filename: str, metadata: str, body: str = "Story.\n") -> None:
    posts = website / "news" / "posts"
    posts.mkdir(parents=True, exist_ok=True)
    (posts / filename).write_text(f"---\n{metadata}---\n{body}")


META = "title: JailBee 1.5\ndate: 2026-09-28\nsummary: Release notes\n"


def test_no_posts_renders_a_readable_empty_index(site: tuple[Path, Path]) -> None:
    website, output = site
    build(website, output)

    html = (output / "news" / "index.html").read_text()
    assert "No news yet" in html
    assert 'href="/"' in html
    assert "JailBee" in html


def test_article_has_body_brand_and_canonical_url(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(
        website,
        "2026-09-28-release.md",
        META,
        "## What changed\n\n- Faster start\n\n[Read docs](/docs/)\n",
    )
    build(website, output)

    html = (output / "news" / "release" / "index.html").read_text()
    assert html.count("<h1") == 1
    assert "<h1>JailBee 1.5</h1>" in html
    assert "<h2>What changed</h2>" in html
    assert "<li>Faster start</li>" in html
    assert '<a href="/docs/">Read docs</a>' in html
    assert '<link rel="canonical" href="https://jailbee.gisgro.io/news/release/"' in html
    assert "2026-09-28" in html
    assert "Release notes" in html
    assert "topbar" in html and "footer" in html
    assert '/assets/style.css?v=' in html and '/assets/news.css?v=' in html
    assert "https://jailbee.gisgro.io/assets/img/jailbee-og.png" in html


def test_feature_image_and_metadata_are_escaped(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(
        website,
        "2026-09-28-release.md",
        'title: "A & <B>"\ndate: 2026-09-28\nsummary: "It has \\"quotes\\" & news"\n'
        'image: /assets/feature.png\nimage_alt: "An <image> & a bee"\n',
    )
    build(website, output)

    article = (output / "news" / "release" / "index.html").read_text()
    archive = (output / "news" / "index.html").read_text()
    for page in (article, archive):
        assert "A &amp; &lt;B&gt;" in page
        assert "An &lt;image&gt; &amp; a bee" in page
        assert 'src="/assets/feature.png"' in page
    assert "https://jailbee.gisgro.io/assets/feature.png" in article
    assert "It has &#34;quotes&#34; &amp; news" in article


@pytest.mark.parametrize(
    "body",
    [
        "![Remote](https://cdn.example.com/image.png)\n",
        '<img src="https://cdn.example.com/image.png">\n',
        "![Missing](/assets/missing.png)\n",
        "# Another title\n",
    ],
)
def test_bad_body_fails_with_article_path(site: tuple[Path, Path], body: str) -> None:
    website, output = site
    _add(website, "2026-09-28-release.md", META, body)
    with pytest.raises(ValueError, match="2026-09-28-release.md"):
        build(website, output)
