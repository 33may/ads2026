"""One logger for the whole server: '12:01:02.123 | INFO | server-1 | message'."""
import os
import sys

from loguru import logger

SERVER_NAME = os.environ.get("SERVER_NAME", "server")

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level:<5}</level> | "
           "<cyan>{extra[server]}</cyan> | {message}",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
logger = logger.bind(server=SERVER_NAME)
