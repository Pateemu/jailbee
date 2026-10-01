"""Generated news pages and syndication, built without a server or network."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
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
    shutil.copy(REPO / "website" / "index.html", website / "index.html")
    (website / "sitemap.xml").write_text(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://jailbee.gisgro.io/</loc></url></urlset>"
    )
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
    assert "/assets/style.css?v=" in html and "/assets/news.css?v=" in html
    assert "https://jailbee.gisgro.io/assets/img/jailbee-og.png" in html


def test_every_news_page_carries_the_home_pages_top_bar(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(website, "2026-09-28-release.md", META)
    build(website, output)

    home = (website / "index.html").read_text()
    version = re.search(r'class="topbar__version">(v[^<]*)<', home)
    assert version is not None
    for page in (
        output / "news" / "index.html",
        output / "news" / "release" / "index.html",
    ):
        html = page.read_text()
        assert html.count('<header class="topbar">') == 1
        # The icons, the source box and the release number all come along.
        assert html.count("<svg") == home[home.index('<header class="topbar">') :].split(
            "</header>"
        )[0].count("<svg")
        assert f'class="topbar__version">{version.group(1)}<' in html
        assert 'href="https://github.com/VRTFinland/jailbee"' in html
        # Links resolve from /news/<slug>/, so none may stay relative.
        header = html[html.index('<header class="topbar">') : html.index("</header>")]
        assert 'href="/"' in header and 'href="/news/"' in header and 'href="/docs/"' in header
        assert 'src="/assets/img/jailbee-mark.png"' in header
        assert 'href="news/"' not in header and 'src="assets/' not in header
        assert "<!--" not in header


def test_the_newest_title_is_in_the_bar_of_every_page_and_the_home_page(
    site: tuple[Path, Path],
) -> None:
    website, output = site
    _add(website, "2026-09-01-old.md", "title: Older\ndate: 2026-09-01\nsummary: s\n")
    _add(website, "2026-10-02-new.md", "title: Newer <1>\ndate: 2026-10-02\nsummary: s\n")
    build(website, output)

    article = (output / "news" / "old" / "index.html").read_text()
    assert 'class="topbar__latest" href="/news/new/"' in article
    assert "Newer &lt;1&gt;" in article and "Older</a>" not in article.split("</header>")[0]
    home = (output / "index.html").read_text()
    assert 'class="topbar__latest" href="news/new/"' in home
    assert home.count("topbar__latest") == 1
    # The committed page stays chip-free; the chip is a build product.
    assert "topbar__latest" not in (website / "index.html").read_text()


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
        "![Remote][picture]\n\n[picture]: https://cdn.example.com/image.png\n",
        "![An [illustrated] update](/assets/missing.png)\n",
        "![Missing][picture]\n\n[picture]: /assets/missing.png\n",
        '<img src="https://cdn.example.com/image.png">\n',
        '<pre><code><script src="https://cdn.example.com/x.js"></script></code></pre>\n',
        "![Missing](/assets/missing.png)\n",
        "# Another title\n",
        "Another title\n=============\n",
        "> # Quoted title\n",
    ],
)
def test_bad_body_fails_with_article_path(site: tuple[Path, Path], body: str) -> None:
    website, output = site
    _add(website, "2026-09-28-release.md", META, body)
    with pytest.raises(ValueError, match=r"2026-09-28-release\.md"):
        build(website, output)


def test_code_examples_are_not_treated_as_raw_html_or_headings(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(
        website,
        "2026-09-28-release.md",
        META,
        '```html\n<img src="https://example.com/demo.png">\n```\n\n'
        "```sh\n# this is a shell comment\n```\n\n"
        '`<img src="example">` is only an example.\n',
    )
    build(website, output)
    html = (output / "news" / "release" / "index.html").read_text()
    assert html.count("<h1") == 1
    assert "&lt;img src=" in html
    assert "# this is a shell comment" in html


@pytest.mark.parametrize("count,expected_pages", [(0, 1), (10, 1), (11, 2), (21, 3)])
def test_pagination_obeys_ten_posts_per_page(
    site: tuple[Path, Path], count: int, expected_pages: int
) -> None:
    website, output = site
    for number in range(count):
        _add(website, f"2026-09-28-post-{number:02}.md", META)
    build(website, output)

    pages = [output / "news" / "index.html"] + [
        output / "news" / "page" / str(n) / "index.html" for n in range(2, expected_pages + 1)
    ]
    assert all(page.is_file() for page in pages)
    assert [page.read_text().count('class="news-card"') for page in pages] == (
        [min(count, 10)] + [min(count - 10 * (n - 1), 10) for n in range(2, expected_pages + 1)]
    )
    assert not (output / "news" / "page" / str(expected_pages + 1)).exists()
    assert '<link rel="canonical" href="https://jailbee.gisgro.io/news/"' in pages[0].read_text()
    if count > 10:
        assert 'href="/news/page/2/"' in pages[0].read_text()
        assert (
            '<link rel="canonical" href="https://jailbee.gisgro.io/news/page/2/"'
            in pages[1].read_text()
        )
        assert 'href="/news/"' in pages[1].read_text()


def test_rss_is_valid_ordered_and_discoverable(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(
        website,
        "2026-09-28-new.md",
        'title: "New & <important>"\ndate: 2026-09-28\nsummary: "A & B"\n',
    )
    _add(website, "2026-09-27-old.md", META.replace("2026-09-28", "2026-09-27"))
    build(website, output)

    tree = ET.parse(output / "news" / "feed.xml")
    items = tree.findall("./channel/item")
    assert [item.findtext("title") for item in items] == ["New & <important>", "JailBee 1.5"]
    assert [item.findtext("link") for item in items] == [
        "https://jailbee.gisgro.io/news/new/",
        "https://jailbee.gisgro.io/news/old/",
    ]
    assert items[0].findtext("guid") == "https://jailbee.gisgro.io/news/new/"
    assert items[0].findtext("description") == "A & B"
    assert items[0].findtext("pubDate") == "Mon, 28 Sep 2026 00:00:00 GMT"
    html = (output / "news" / "index.html").read_text()
    assert 'type="application/rss+xml"' in html
    assert 'href="/news/feed.xml"' in html


def _sitemap_urls(output: Path) -> set[str]:
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tree = ET.parse(output / "sitemap.xml")
    return {loc.text or "" for loc in tree.findall(".//s:loc", namespace)}


def test_sitemap_contains_only_current_news_pages(site: tuple[Path, Path]) -> None:
    website, output = site
    for number in range(11):
        _add(website, f"2026-09-28-post-{number:02}.md", META)
    build(website, output)
    assert _sitemap_urls(output) == {
        "https://jailbee.gisgro.io/",
        "https://jailbee.gisgro.io/news/",
        "https://jailbee.gisgro.io/news/page/2/",
        *(f"https://jailbee.gisgro.io/news/post-{number:02}/" for number in range(11)),
    }

    (output / "index.html").write_text("Home")
    docs = output / "docs" / "index.html"
    docs.parent.mkdir()
    docs.write_text("Docs")
    for number in range(1, 11):
        (website / "news" / "posts" / f"2026-09-28-post-{number:02}.md").unlink()
    build(website, output)
    # The home page is rewritten from its source, with the newest title in the bar;
    # the docs are never touched.
    assert "topbar__latest" in (output / "index.html").read_text()
    assert docs.read_text() == "Docs"
    assert not (output / "news" / "page" / "2").exists()
    assert not (output / "news" / "post-01").exists()
    assert _sitemap_urls(output) == {
        "https://jailbee.gisgro.io/",
        "https://jailbee.gisgro.io/news/",
        "https://jailbee.gisgro.io/news/post-00/",
    }


def test_builder_does_not_copy_editorial_sources_into_output(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(website, "2026-09-28-release.md", META)
    (website / "news" / "README.md").write_text("Editing instructions")
    build(website, output)
    assert sorted(p.relative_to(output) for p in output.rglob("*.md")) == []
    assert not (output / "news" / "templates").exists()


def test_cli_returns_path_specific_diagnostic_on_invalid_article(site: tuple[Path, Path]) -> None:
    website, output = site
    _add(website, "2026-09-28-bad.md", "title: Missing date\nsummary: Summary\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.news_site",
            "--website-dir",
            str(website),
            "--site-dir",
            str(output),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "2026-09-28-bad.md" in result.stderr
    assert "date" in result.stderr
