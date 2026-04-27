"""
Setup logger sederhana - log ke file dan console.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from src import config


def setup_logger(name: str = "inventory_bot") -> logging.Logger:
    """Buat logger dengan rotating file handler + console handler."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler dengan rotation (5 MB x 3 backup)
    os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)
    fh = RotatingFileHandler(
        config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


logger = setup_logger()
