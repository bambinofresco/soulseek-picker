import logging

import uvicorn

from .config import get_settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("picker.main:app", host="0.0.0.0", port=get_settings().port, proxy_headers=True)


if __name__ == "__main__":
    main()
