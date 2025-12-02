#!/usr/bin/python3

import argparse
import logging
import os
import pathlib
import sys

import jellyplex as jp


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a Plex compatible media library from a Jellyfin library.")
    parser.add_argument("source", help="Jellyfin media library")
    parser.add_argument("target", help="Plex media library")
    parser.add_argument("--convert-to", type=str,
        choices=[jp.JellyfinLibrary.shortname(), jp.PlexLibrary.shortname(), "auto"], default="auto",
        help="Type of library to convert to ('auto' will try to determine source library type)")
    parser.add_argument("--dry-run", action="store_true", help="Show actions only, don't execute them")
    parser.add_argument("--delete", action="store_true", help="Remove stray folders from target library")
    parser.add_argument("--create", action="store_true", help="Create missing target library")
    parser.add_argument("--verbose", action="store_true", help="Show more information messages")
    parser.add_argument("--debug", action="store_true", help="Show debug messages")
    parser.add_argument("--radarr", action="store_true", help="Enable Radarr hook mode (requires radarr environment variables)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s: %(asctime)s -- %(message)s",
    )

    include_dirs = []
    if args.radarr:
        event = os.environ.get("radarr_eventtype")
        if not event:
             logging.error("Radarr hook enabled but 'radarr_eventtype' not found in environment.")
             sys.exit(1)

        logging.info("Radarr event: %s", event)
        if event == "Test":
             logging.info("Radarr test successful.")
             sys.exit(0)

        movie_path_str = os.environ.get("radarr_movie_path")
        if not movie_path_str:
             logging.error("Radarr hook: 'radarr_movie_path' not found.")
             sys.exit(1)

        # Resolve paths
        source_path = pathlib.Path(args.source).resolve()
        movie_path = pathlib.Path(movie_path_str).resolve()

        # Radarr passes the movie folder path.
        # We need to ensure it is inside the source library.
        try:
             # is_relative_to is Python 3.9+
             if not movie_path.is_relative_to(source_path):
                 logging.error("Movie path '%s' is not within source library '%s'", movie_path, source_path)
                 sys.exit(1)
        except AttributeError:
             # Fallback for Python < 3.9 just in case, though project requires >= 3.12
             try:
                 movie_path.relative_to(source_path)
             except ValueError:
                 logging.error("Movie path '%s' is not within source library '%s'", movie_path, source_path)
                 sys.exit(1)

        logging.info("Radarr hook: syncing only '%s'", movie_path)
        include_dirs.append(movie_path)

    result = 0
    try:
        result = jp.sync(
            args.source,
            args.target,
            dry_run= args.dry_run,
            delete=args.delete,
            create=args.create,
            verbose=args.verbose,
            debug=args.debug,
            convert_to=args.convert_to,
            include_dirs=include_dirs if include_dirs else None,
        )
    except KeyboardInterrupt:
        logging.info("INTERRUPTED")
        result = 10
    except Exception as exc:
        logging.error("Exception: %s", exc)
        result = 99
    sys.exit(result)


if __name__ == "__main__":
    main()
