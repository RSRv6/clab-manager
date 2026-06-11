import logging
import os
import gzip
import shutil
from logging.handlers import RotatingFileHandler


class CompressedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that compresses rotated backup files to .gz"""

    def doRollover(self):
        """Rotate, then compress the *newly created* backup file .1 → .1.gz."""
        super().doRollover()
        # After super().doRollover(), the freshly rotated file is always .1
        # (and older backups have already been shifted to .2, .3 … by super).
        # We compress each numbered backup that isn’t already gzipped.
        if self.backupCount > 0:
            for i in range(1, self.backupCount + 1):
                src = f"{self.baseFilename}.{i}"
                dst = f"{src}.gz"
                if os.path.exists(src):
                    with open(src, "rb") as f_in, gzip.open(dst, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                    os.remove(src)


def setup_logging() -> None:
    level_name = os.getenv("CENTRAL_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_dir = os.getenv("CENTRAL_LOG_DIR", "").strip()
    if not log_dir:
        # Default: <repo_root>/log
        log_dir = os.path.join(os.path.dirname(__file__), "..", "..", "log")
        log_dir = os.path.normpath(log_dir)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "central.log")

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    # Max 3MB per file, keep max 7 backups = ~20MB total (compressed much smaller)
    file_handler = CompressedRotatingFileHandler(log_file, maxBytes=3_000_000, backupCount=7)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)
