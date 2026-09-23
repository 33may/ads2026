"""Fetch Project Gutenberg novels as plain text: python fetch_gutenberg.py [N]

Downloads candidate books by id, strips the Gutenberg header/footer, keeps the
first N whose length falls in the MIN..MAX word band, saves texts/gutenberg/<slug>.txt.
"""
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

URL = "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt"
HEADERS = {"User-Agent": "ads-wordcount-corpus/1.0 (TU/e 2IMN10 lab; contact via course)"}
OUT = Path(__file__).parent / "gutenberg"
MIN_WORDS, MAX_WORDS = 50_000, 200_000
WORD = re.compile(r"\w+")

# Well-known English novels on Project Gutenberg, roughly by popularity. Ids that
# fail, are not English prose, or fall outside the word band are simply skipped.
CANDIDATES = [
    1342, 84, 2701, 1661, 11, 98, 174, 345, 76, 1400, 5200, 64317, 2554, 46, 43, 16,
    120, 219, 1260, 768, 158, 161, 105, 141, 121, 2814, 35, 36, 244, 2852, 3070, 834,
    221, 108, 25344, 55, 113, 236, 271, 289, 74, 2500, 730, 766, 1023, 963, 205, 940,
    1080, 41, 1952, 2148, 42, 209, 4300, 33, 45, 514, 1250, 21, 2591, 1727, 6130, 8800,
    28054, 2680, 1998, 3600, 4363, 1232, 1497, 30254, 2413, 1322, 3300, 100, 2600,
    1184, 135, 996, 155, 583, 155, 1155, 863, 8492, 3268, 145, 1399, 2638, 599, 1013,
    140, 1257, 2097, 786, 580, 967, 883, 700, 821, 653, 1416, 32, 62, 1064, 2542, 58,
    215, 910, 5230, 19942, 1837, 20, 2892, 3825, 1112, 1524, 2265, 1513, 1531, 6593,
    25305, 4276, 12, 19, 20203, 8578, 41445, 24022, 730, 44, 2199, 3090, 6761, 502,
    17396, 61262, 27827, 18857, 103, 164, 14838, 1268, 2609, 22381,
    # more novels
    564, 917, 767, 969, 9182, 110, 153, 107, 3044, 2350, 139, 159, 86, 3176, 245,
    421, 550, 6688, 507, 3409, 619, 394, 2153, 2833, 2870, 177, 541, 284, 77, 8291,
    27681, 82, 5998, 2610, 3526, 8117, 2226, 974, 2021, 526, 479, 54, 1164, 2166,
    3155, 233, 521, 370, 829, 1079, 805, 9830, 4217, 4240, 2891, 2641, 47, 51, 131,
    17396, 8954, 14851, 903, 60, 95, 558, 543, 1156, 24, 242, 5946, 1622, 4085, 2523,
    2160, 8409, 1152, 1240, 2542, 1447, 2775, 15, 2226, 3178, 4363,
    42671, 2027, 12, 1206, 3268, 2776, 1013, 6120, 2884, 7370, 1245, 271, 289,
]

START = re.compile(r"\*\*\* ?START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I)
END = re.compile(r"\*\*\* ?END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I)


def fetch(book_id):
    """(id, title, body) or None if the book is unusable."""
    try:
        req = urllib.request.Request(URL.format(id=book_id), headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8-sig", errors="replace")
    except Exception as e:
        print(f"{book_id}\tfetch failed: {e}", file=sys.stderr)
        return None
    m = re.search(r"^Title:\s*(.+)$", raw, re.M)
    title = m.group(1).strip() if m else f"book-{book_id}"
    s, e = START.search(raw), END.search(raw)
    if not s or not e:
        print(f"{book_id}\t{title}: no header/footer markers", file=sys.stderr)
        return None
    body = raw[s.end():e.start()].strip()
    return book_id, title, body


def slugify(title):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", title.lower())).strip("-")[:60]


MANIFEST = OUT / "manifest.tsv"      # id <tab> reference <tab> words — what is in the corpus


def main(n):
    OUT.mkdir(exist_ok=True)
    done = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            book_id, ref, words = line.split("\t")
            done[int(book_id)] = ref
    seen = set(done)
    ids = [i for i in CANDIDATES if not (i in seen or seen.add(i))]
    kept = len(done)
    with ThreadPoolExecutor(max_workers=3) as pool, MANIFEST.open("a") as manifest:
        for res in pool.map(fetch, ids):
            if res is None or kept == n:
                continue
            book_id, title, body = res
            words = len(WORD.findall(body))
            if not MIN_WORDS <= words <= MAX_WORDS:
                print(f"{book_id}\t{title}: {words} words, skipped", file=sys.stderr)
                continue
            ref = slugify(title)
            if ref in done.values():
                ref = f"{ref}-{book_id}"
            (OUT / f"{ref}.txt").write_text(body + "\n", encoding="utf-8")
            manifest.write(f"{book_id}\t{ref}\t{words}\n")
            manifest.flush()
            done[book_id] = ref
            kept += 1
            print(f"{kept:3d}\t{ref}\t{words} words", flush=True)
    print(f"corpus: {kept} books in {OUT}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 100)
