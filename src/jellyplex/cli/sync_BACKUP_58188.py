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
<<<<<<< HEAD
    parser.add_argument("--update-filenames", action="store_true", help="Rename existing hardlinks if they have outdated names")
=======
    parser.add_argument("--partial", type=str, metavar="PATH",
        help="Sync only the specified movie folder (partial sync)")
    parser.add_argument("--radarr-hook", action="store_true",
        help="Read movie path from Radarr environment variables")
>>>>>>> 21da540 (Add Radarr hook and partial sync support)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s: %(asctime)s -- %(message)s",
    )

    result = 0
    try:
        partial_path = None

        if args.radarr_hook:
            event_type = os.environ.get("radarr_eventtype", "")
            movie_title = os.environ.get("radarr_movie_title", "Unknown")

            # Handle test events from Radarr
            if event_type == "Test":
                logging.info("Radarr test event received, exiting successfully")
                sys.exit(0)

            # Only process import-related events
            if event_type not in ("Download", "Upgrade", "Rename"):
                logging.info(f"Ignoring Radarr event type: {event_type}")
                sys.exit(0)

            partial_path = os.environ.get("radarr_movie_path")
            if not partial_path:
                logging.error("radarr_movie_path environment variable not set")
                sys.exit(1)

            logging.info(f"Radarr hook: {event_type} - {movie_title}")

        elif args.partial:
            partial_path = args.partial

        result = jp.sync(
            args.source,
            args.target,
            partial_path=partial_path,
            dry_run=args.dry_run,
            delete=args.delete,
            create=args.create,
            verbose=args.verbose,
            debug=args.debug,
            convert_to=args.convert_to,
            update_filenames=args.update_filenames,
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
