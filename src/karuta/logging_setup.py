from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(level: str = "INFO", log_dir: Path = Path("./logs")) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:HH:mm:ss}</green> "
            "<level>{level: <7}</level> "
            "<cyan>{extra[account]: <3}</cyan> "
            "<level>{message}</level>"
        ),
        filter=_with_default_account,
    )
    logger.add(
        log_dir / "karuta.log",
        level="DEBUG",
        rotation="10 MB",
        retention=5,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {extra[account]: <3} | {message}",
        filter=_with_default_account,
        enqueue=True,
    )


def _with_default_account(record: dict) -> bool:
    record["extra"].setdefault("account", "-")
    return True


def account_logger(account_id: str):
    return logger.bind(account=account_id)
