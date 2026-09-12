"""Watch the Tier 3 cursor work, without letting it change anything.

    uv run python scripts/demo_cursor.py

Brings TallyPrime to the front, then walks the red ring around its window with
captions, pressing only keys that cannot alter data (Escape). Nothing here goes
near the approval gate because nothing here mutates - it exists so you can see
what the narration looks like before you ever switch the fallback on.
"""

from __future__ import annotations

import argparse
import sys
import time

from tallyagent_agent.fallback import spotlight as spotlight_mod
from tallyagent_agent.fallback import window as window_mod


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shot", default="", help="save a screenshot here, mid-demo"
    )
    parser.add_argument("--no-window", action="store_true", help="narrate to the log")
    args = parser.parse_args()

    bounds, reason = window_mod.ensure_visible()
    if bounds is None:
        print(reason, file=sys.stderr)
        return 1
    print(f"Tally is at {bounds.left},{bounds.top} {bounds.width}x{bounds.height}")

    light = spotlight_mod.build(show_cursor=not args.no_window)
    try:
        middle_x = bounds.left + bounds.width // 2
        middle_y = bounds.top + bounds.height // 2

        # Pointing, never clicking: a blind click in someone's live books
        # could navigate anywhere, and this demo has no business doing that.
        light.announce("reading the screen before deciding anything")
        light.point(middle_x, bounds.top + 60)

        if args.shot:
            _screenshot(args.shot, bounds)
            print(f"screenshot: {args.shot}")

        light.announce("this is where the Day Book would open")
        light.point(bounds.left + 120, middle_y)

        light.announce("and this is what a delete would look like")
        light.point(
            middle_x,
            middle_y,
            spotlight_mod.describe("key", "Alt+D", "remove the duplicate receipt"),
        )
        time.sleep(1.0)
    finally:
        light.close()
    print("done; nothing was altered")
    return 0


def _screenshot(path: str, bounds) -> None:  # type: ignore[no-untyped-def]
    try:
        import mss
        import mss.tools
    except ImportError:
        print("mss not installed; no screenshot", file=sys.stderr)
        return
    with mss.mss() as shot:
        # Grab the Tally window's own rectangle. Monitor indexes are a trap on
        # a multi-display machine: monitors[0] is the union of them all and can
        # come back black, and the primary is not always index 1.
        image = shot.grab(
            {
                "left": bounds.left,
                "top": bounds.top,
                "width": bounds.width,
                "height": bounds.height,
            }
        )
        mss.tools.to_png(image.rgb, image.size, output=path)


if __name__ == "__main__":
    raise SystemExit(main())
