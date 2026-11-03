"""Manual one-time importer; run from a trusted machine with DATABASE_URL set."""

import argparse

from postgres_storage import import_registry_file, import_tracking_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tracking", required=True, help="Existing bot tracking JSON")
    parser.add_argument("--registry", required=True, help="amazon_deals_sent_registry.json")
    parser.add_argument("--bot-id", default="bot1")
    args = parser.parse_args()

    compressed_bytes = import_tracking_file(args.tracking, args.bot_id)
    imported_alerts = import_registry_file(args.registry, args.bot_id)
    print(f"Tracking row ready for {args.bot_id}: {compressed_bytes:,} compressed bytes.")
    print(f"Imported or already present alert records: {imported_alerts:,}.")


if __name__ == "__main__":
    main()

