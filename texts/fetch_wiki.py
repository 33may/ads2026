"""Fetch a Wikipedia page plus its first N body links as plain text: python fetch_wiki.py "Title" N"""
import html
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "ads-wordcount-corpus/1.0 (TU/e 2IMN10 lab; contact via course)"}
SKIP_NS = ("File:", "Category:", "Help:", "Wikipedia:", "Special:", "Template:", "Portal:", "Talk:")
MIN_CHARS = 2000
OUT = Path(__file__).parent


def api(**params):
    url = API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS)) as r:
        return json.load(r)


def extract(title):
    """Plain-text extract of a page, or None if missing/disambiguation/stub."""
    pages = api(action="query", prop="extracts|pageprops", explaintext=1, redirects=1, titles=title)["query"]["pages"]
    page = next(iter(pages.values()))
    text = re.sub(r"^=+ *(.*?) *=+$", r"\1", page.get("extract", ""), flags=re.M)  # "== Heading ==" -> "Heading"
    if "missing" in page or "disambiguation" in page.get("pageprops", {}) or len(text) < MIN_CHARS:
        return None
    return text


def body_links(title):
    """Article titles linked from <p> elements of the page's main content, in order, deduplicated."""
    doc = api(action="parse", page=title, prop="text", redirects=1)["parse"]["text"]["*"]
    seen, out = set(), []
    for para in re.findall(r"<p\b[^>]*>(.*?)</p>", doc, re.S):
        for href in re.findall(r'<a href="/wiki/([^"#?]+)"', para):
            name = html.unescape(urllib.parse.unquote(href)).replace("_", " ")
            if name.startswith(SKIP_NS) or name == title or name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


def slugify(title):
    return re.sub(r"[^a-z0-9-]", "", title.lower().replace(" ", "-"))


def save(title, text):
    path = OUT / f"{slugify(title)}.txt"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    print(f"{path.stem}\t{len(text)} chars\t{len(text.split())} words")


def main(title, n):
    save(title, extract(title))
    done = 0
    for link in body_links(title):
        if done == n:
            break
        text = extract(link)
        if text is None:
            continue
        save(link, text)
        done += 1


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
