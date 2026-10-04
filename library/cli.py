#!/usr/bin/env python3
"""Command-line front-end for the media library (./media.sh list|add|delete|orphans|import)."""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
from core import LibraryError, human  # noqa: E402


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise LibraryError("refusing to continue without a terminal; pass --yes")
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def choose(items: list, label, assume_yes: bool, what: str):
    if not items:
        raise LibraryError(f"no {what} matched")
    if len(items) == 1:
        return items[0]
    if assume_yes:
        raise LibraryError(f"{len(items)} {what}s matched; be more specific or use an ID")
    for i, item in enumerate(items, 1):
        print(f"  {i}) {label(item)}")
    while True:
        ans = input(f"Pick 1-{len(items)} (blank to cancel): ").strip()
        if not ans:
            sys.exit("Cancelled.")
        if ans.isdigit() and 1 <= int(ans) <= len(items):
            return items[int(ans) - 1]


def find_in_library(k: core.Kind, query: str) -> list[dict]:
    items = k.items()
    prefix = k.id_name + ":"
    if query.lower().startswith(prefix):
        want = int(query[len(prefix):])
        return [i for i in items if i.get(k.id_key) == want]
    q = query.lower()
    exact = [i for i in items if i["title"].lower() == q]
    return exact or [i for i in items if q in i["title"].lower()]


def cmd_list(stack, _args) -> None:
    data = core.overview(stack)
    for key, name in (("movies", "Movies"), ("shows", "Shows")):
        print(f"\n{name} ({len(data[key])}):")
        for r in data[key]:
            size = r["sizeHuman"] or "not downloaded"
            extra = f"  episodes {r['episodes']}" if "episodes" in r else ""
            print(f"  {r['label']:55}  {size:>15}  {r['status']}{extra}")
    o = data["orphans"]
    if o["count"]:
        print(f"\n{o['count']} orphan torrent(s), {o['sizeHuman']}: not linked to any title "
              "(clean up with: ./media.sh orphans --delete)")


def cmd_add(stack, args) -> None:
    k = stack.kind(args.kind)
    query = " ".join(args.query)
    results = k.lookup(query)
    if args.year:
        results = [r for r in results if r.get("year") == args.year]
    results = results[:8]
    if args.yes and len(results) > 1:
        exact = [r for r in results if r["title"].lower() == query.lower()]
        results = exact[:1] if len(exact) == 1 else results
    pick = choose(results, lambda r: k.label(r) + ("  (already added)" if r.get("id") else ""),
                  args.yes, k.kind)
    res = core.add_title(stack, args.kind, pick[k.id_key])
    print(res["message"])
    print("Waiting up to 60 s for a release to be grabbed...")
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(5)
        titles = core.grabbed(stack, args.kind, res["id"])
        if titles:
            print("Grabbed: " + "; ".join(titles))
            print("subgate now checks it for English subs/audio: ./media.sh logs subgate")
            return
    print(f"Nothing grabbed yet. {k.arr.name} keeps looking (RSS + scheduled searches); "
          f"check Activity -> History in {k.arr.name}, or run this again later to re-search.")


def cmd_delete(stack, args) -> None:
    k = stack.kind(args.kind)
    item = choose(find_in_library(k, " ".join(args.query)), k.label, args.yes,
                  f"{k.kind} in {k.arr.name}")
    plan = core.delete_plan(stack, args.kind, item["id"])
    print(f"\nThis will permanently delete {plan['label']}:")
    print(f"  - {plan['arr']} entry + library folder {plan['path']} ({plan['sizeHuman']})")
    for t in plan["torrents"]:
        print(f"  - torrent '{t['name']}' and its data ({t['sizeHuman']}, {t['state']})")
    if not plan["torrents"]:
        print("  - (no torrent left in qBittorrent)")
    print("  - Seerr request/availability, so it can be requested again")
    if args.exclude:
        print(f"  - and add it to {plan['arr']}'s import exclusions (lists won't re-add it)")
    if not confirm("Proceed?", args.yes):
        sys.exit("Cancelled.")
    res = core.delete_execute(stack, plan, args.exclude, log=lambda m: print("  " + m))
    if not res["ok"]:
        print("NOT fully deleted:\n  - " + "\n  - ".join(res["problems"]))
        sys.exit(1)
    print(f"Deleted {res['label']}; ~{res['freedHuman']} freed. "
          "Jellyfin drops it on its next real-time scan.")


def cmd_orphans(stack, args) -> None:
    orphans = core.orphan_list(stack)
    if not orphans:
        print("No orphan torrents.")
        return
    print("Torrents not linked to any movie/show in Radarr/Sonarr:")
    for t in orphans:
        print(f"  - {t['name']}  ({t['sizeHuman']}, {t['category']}, {t['state']})")
    print(f"Total: {human(sum(t['size'] for t in orphans))}")
    if not args.delete:
        print("Run with --delete to remove them and their data.")
        return
    if not confirm(f"Delete these {len(orphans)} torrent(s) and their data?", args.yes):
        sys.exit("Cancelled.")
    res = core.orphans_delete(stack, [t["hash"] for t in orphans])
    if not res["ok"]:
        print("NOT fully deleted: " + ", ".join(res["still"]))
        sys.exit(1)
    print(f"Deleted; ~{res['freedHuman']} freed.")


def cmd_import(stack, args) -> None:
    path = os.path.abspath(args.path)
    if not os.path.exists(path):
        raise LibraryError(f"{args.path} does not exist")
    query = " ".join(args.title) if args.title else None
    cand = core.import_candidates(stack, args.kind, path, query, args.year)
    if not query:
        if not cand["guess"]:
            raise LibraryError("couldn't guess the title from the filename; pass --title")
        print(f"Guessed from filename: {cand['guess']}"
              + (f" ({cand['year']})" if cand["year"] else ""))
    results = cand["results"]
    if args.yes and len(results) > 1:
        q = (query or cand["guess"]).lower()
        exact = [r for r in results if r["title"].lower() == q
                 and (not cand["year"] or r["year"] == cand["year"])]
        results = exact[:1] or results[:1]
    pick = choose(results, lambda r: r["label"], False, args.kind)
    if not args.yes and len(results) == 1 and not confirm(f"Import as {pick['label']}?", False):
        sys.exit("Cancelled.")

    plan = core.import_plan(stack, args.kind, path, pick["extId"], args.replace)
    print(f"\nImporting into {plan['folder']}:")
    for f in plan["files"]:
        tag = f.get("tag", "")
        print(f"  {tag + '  ' if tag else ''}{f['name']}  ({f['sizeHuman']})")
    for s in plan["skipped"]:
        print(f"  SKIP  {s['name']}: {s['why']}")
    for w in plan["warnings"]:
        print(f"  NOTE: {w}")
    if not confirm("Proceed?", args.yes):
        sys.exit("Cancelled.")
    res = core.import_execute(stack, args.kind, path, pick["extId"], args.replace,
                              args.monitor, log=lambda m: print("  " + m))
    print(f"Imported {res['label']} -> {res['imported']}. "
          "Jellyfin picks it up within a minute (real-time monitoring).")


def main() -> None:
    ap = argparse.ArgumentParser(prog="media.sh", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="movies/shows with size and torrent state")
    for name in ("add", "delete"):
        p = sub.add_parser(name)
        p.add_argument("kind", choices=["movie", "show"])
        p.add_argument("query", nargs="+", help="title, or tmdb:ID (movie) / tvdb:ID (show)")
        p.add_argument("--yes", action="store_true", help="no prompts")
        if name == "add":
            p.add_argument("--year", type=int)
        else:
            p.add_argument("--exclude", action="store_true", help="also add an import exclusion")
    p = sub.add_parser("orphans", help="torrents not linked to any title")
    p.add_argument("--delete", action="store_true")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("import", help="copy local files into the library")
    p.add_argument("kind", choices=["movie", "show"])
    p.add_argument("path", help="video file, or a folder (movie folder / season folder)")
    p.add_argument("--title", nargs="+", help="title or tmdb:ID / tvdb:ID (default: guess)")
    p.add_argument("--year", type=int)
    p.add_argument("--replace", action="store_true", help="replace existing library files")
    p.add_argument("--monitor", action="store_true",
                   help="movie: stay monitored so Radarr may upgrade it later")
    p.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    try:
        stack = core.Stack()
        {"list": cmd_list, "add": cmd_add, "delete": cmd_delete, "orphans": cmd_orphans,
         "import": cmd_import}[args.cmd](stack, args)
    except LibraryError as e:
        sys.exit(f"ERROR: {e}")
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")


if __name__ == "__main__":
    main()
