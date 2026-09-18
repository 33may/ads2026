"""End-user commands.  Usage: python -m client.cli <command> [args]"""
import argparse

from client.api import connect, NoSuchReference


def cmd_ping(args):
    with connect() as c:
        print(c.ping())


def cmd_query(args):
    with connect() as c:
        try:
            n = c.get_count(args.keyword, args.ref)
        except NoSuchReference:
            print(f"no such reference: {args.ref}")
            return
        print(f"{n}  ({c.last_ms:.1f} ms)")


def cmd_list(args):
    with connect() as c:
        for ref in c.list_references(args.query):
            print(ref)


def main():
    parser = argparse.ArgumentParser(prog="client")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ping", help="round trip to the service").set_defaults(func=cmd_ping)

    p = sub.add_parser("query", help="count a keyword in a reference")
    p.add_argument("keyword")
    p.add_argument("ref")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("list", help="available references")
    p.add_argument("--query", default="", help="prefix filter")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
