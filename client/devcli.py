"""Developer commands, normally run through make.  Usage: python -m client.devcli <command> [args]"""
import argparse
from pathlib import Path

from client.api import connect


def cmd_seed(args):
    with connect() as c:
        for path in sorted(args.dir.glob("*.txt")):
            text = path.read_text(encoding="utf-8")
            c.upload_text(path.stem, text)
            print(f"{path.stem}: {len(text)} chars")


def cmd_delete(args):
    with connect() as c:
        c.delete_text(args.ref)


def cmd_get(args):
    with connect() as c:
        try:
            print(c.get_text(args.ref))
        except KeyError:
            print(f"no such reference: {args.ref}")


def cmd_hot(args):
    with connect() as c:
        for kw, n in c.hot_keywords(args.n):
            print(f"{n:6d}  {kw}")


def cmd_cache_flush(args):
    with connect() as c:
        c.cache_flush()


def main():
    parser = argparse.ArgumentParser(prog="devclient")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="upload every *.txt in a folder")
    p.add_argument("--dir", type=Path, default=Path("texts"))
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("delete", help="remove one text")
    p.add_argument("ref")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("get", help="fetch one text")
    p.add_argument("ref")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("hot", help="most requested keywords")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(func=cmd_hot)

    sub.add_parser("cache-flush", help="empty the cache (counts, texts, hot keywords)").set_defaults(func=cmd_cache_flush)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
