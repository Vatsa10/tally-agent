"""Put a local TallyPrime back to answering.

    uv run python scripts/recover_tally.py

A thin front end for ``tallyagent_tally.recovery``, which is the same code the
client calls by itself when a request fails - so what this script fixes by hand
is exactly what a session fixes on its own. Prefer ``tallyagent doctor``, which
runs this plus the rest of the checks.

Exits 0 when a company is loaded and the port answers.
"""

from __future__ import annotations

import argparse
import asyncio

from tallyagent_tally.client import TallyConfig
from tallyagent_tally.recovery import Recovery


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--attempts",
        type=int,
        default=4,
        help="How many times to clear the licence screen.",
    )
    args = parser.parse_args()

    healer = Recovery(
        TallyConfig(host=args.host, port=args.port), attempts=args.attempts
    )
    result = await healer.repair(force=True)
    print(result.describe())
    if not result.healthy:
        return 1
    print("companies:", ", ".join(await healer.companies()))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
