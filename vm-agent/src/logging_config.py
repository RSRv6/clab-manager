import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging() -> None:
    level_name = os.getenv("VM_AGENT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    paramiko_level_name = os.getenv("VM_AGENT_PARAMIKO_LOG_LEVEL", "WARNING").upper()
    paramiko_level = getattr(logging, paramiko_level_name, logging.WARNING)
    paramiko_transport_level_name = os.getenv("VM_AGENT_PARAMIKO_TRANSPORT_LOG_LEVEL", "CRITICAL").upper()
    paramiko_transport_level = getattr(logging, paramiko_transport_level_name, logging.CRITICAL)

    log_dir = os.getenv("VM_AGENT_LOG_DIR", "/opt/virtual-labs-management/log")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "vm-agent.log")

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    # Paramiko can emit verbose transport tracebacks for transient banner resets.
    # We keep retries at app level and reduce transport noise by default.
    logging.getLogger("paramiko").setLevel(paramiko_level)
    transport_logger = logging.getLogger("paramiko.transport")
    transport_logger.setLevel(paramiko_transport_level)
