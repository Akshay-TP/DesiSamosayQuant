"""Summarize successful live order actions by UTC date for competition review."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def summarize(path: Path) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    malformed = 0
    if not path.exists():
        return counts, malformed

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if event.get("event") != "order_result" or not event.get("success") or event.get("dry_run"):
                continue
            timestamp = event.get("time")
            if not timestamp:
                malformed += 1
                continue
            try:
                moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            except ValueError:
                malformed += 1
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            counts[moment.astimezone(timezone.utc).date().isoformat()] += 1
    return counts, malformed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=Path("runtime/logs/orders.jsonl"))
    args = parser.parse_args()
    counts, malformed = summarize(args.log)
    if not args.log.exists():
        print(f"No order log found at {args.log}; the bot has not recorded orders yet.")
        return 0
    print("Successful live order actions by UTC date (dry runs excluded):")
    if not counts:
        print("  No successful live order actions recorded.")
    for day, count in sorted(counts.items()):
        print(f"  {day}: {count}")
    print(f"Active dates recorded: {len(counts)}")
    print("The organizers have not published a numeric 'enough trades' threshold on the event page.")
    if malformed:
        print(f"Skipped malformed or undated log entries: {malformed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
