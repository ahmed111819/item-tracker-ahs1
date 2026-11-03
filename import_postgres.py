"""Manual one-time importer; run from a trusted machine with DATABASE_URL set."""

import argparse

from postgres_storage import import_registry_file, import_tracking_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tracking", action="append", required=True,
        help="Existing tracking file as BOT_ID=PATH; repeat for bot1, bot2, and bot3",
    )
    parser.add_argument("--registry", required=True, help="amazon_deals_sent_registry.json")
    args = parser.parse_args()

    tracking_files = {}
    for item in args.tracking:
        if "=" not in item:
            parser.error("Each --tracking value must be BOT_ID=PATH")
        bot_id, path = item.split("=", 1)
        if bot_id in tracking_files:
            parser.error(f"Duplicate tracking file for {bot_id}")
        tracking_files[bot_id] = path
    required_bots = {"bot1", "bot2", "bot3"}
    if set(tracking_files) != required_bots:
        parser.error("Provide exactly one tracking JSON for each of bot1, bot2, and bot3")

    for bot_id in sorted(required_bots):
        compressed_bytes = import_tracking_file(tracking_files[bot_id], bot_id)
        print(f"Tracking row ready for {bot_id}: {compressed_bytes:,} compressed bytes.")
    imported_alerts = import_registry_file(args.registry)
    print(f"Imported or already present alert records: {imported_alerts:,}.")


if __name__ == "__main__":
    main()

