"""python -m picker            run the web app
python -m picker probe      check the real services' API shapes (see picker/probe.py)
"""

import asyncio
import logging
import os
import sys

import uvicorn

from .config import get_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.argv[1:2] == ["probe"]:
        sys.exit(asyncio.run(_probe()))
    uvicorn.run("picker.main:app", host="0.0.0.0", port=get_settings().port, proxy_headers=True)


def _drop_root() -> None:
    """`docker exec` runs as root; write /config/probe as PUID:PGID like the app does."""
    if hasattr(os, "geteuid") and os.geteuid() == 0 and os.environ.get("PUID"):
        os.setgid(int(os.environ.get("PGID", "100")))
        os.setuid(int(os.environ["PUID"]))


async def _probe() -> int:
    from .clients import close_services
    from .probe import run_probe

    _drop_root()

    try:
        report = await run_probe(get_settings())
    finally:
        await close_services()
    print(report.text())
    return 1 if any(f.level in ("mismatch", "error") for f in report.findings) else 0


if __name__ == "__main__":
    main()
