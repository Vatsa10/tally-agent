"""Windows tray icon.

Optional: without the desktop extra the daemon still runs headless and says so.
The icon is drawn rather than shipped as a file so the PyInstaller bundle stays
a single executable with no data files to lose.
"""

from __future__ import annotations

import logging
import threading
import webbrowser

log = logging.getLogger(__name__)


def _icon_image(pending: int):  # type: ignore[no-untyped-def]
    """A small badge: dark when idle, amber when something needs approval."""
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    colour = (217, 119, 6) if pending else (15, 23, 42)
    draw.ellipse((4, 4, size - 4, size - 4), fill=colour)
    draw.text((22, 20), "T", fill=(255, 255, 255))
    return image


def run_tray(url: str, status) -> threading.Thread | None:  # type: ignore[no-untyped-def]
    """Start the tray icon on its own thread.

    Returns None when pystray/Pillow are absent - the daemon is still fully
    usable at ``url``, which is what the caller tells the user.
    """
    try:
        import pystray  # type: ignore[import-not-found]
    except ImportError:
        log.info("pystray not installed; running headless. Open %s", url)
        return None

    def build_menu() -> object:
        return pystray.Menu(
            pystray.MenuItem("Open tallyagent", lambda: webbrowser.open(url)),
            pystray.MenuItem(
                lambda _item: f"Awaiting approval: {status().get('pending', 0)}",
                lambda: webbrowser.open(url),
            ),
            pystray.MenuItem("Quit", lambda icon, _item: icon.stop()),
        )

    icon = pystray.Icon(
        "tallyagent",
        _icon_image(status().get("pending", 0)),
        "tallyagent",
        build_menu(),
    )
    thread = threading.Thread(target=icon.run, daemon=True, name="tallyagent-tray")
    thread.start()
    return thread
