import logging
import time, os
from netmiko import ConnectHandler

logger = logging.getLogger(__name__)


def enable_debug(cube_config: dict = None) -> bool:
    """
    SSH into the CUBE and enable SIP debug capturing.
    Returns True if successful. False otherwise.
    """
    config = cube_config
    logger.info(f"Connecting to CUBE at {config['host']} to enable debug...")

    try:
        with ConnectHandler(**config) as conn:
            conn.enable()
            conn.send_command("terminal length 0")
            conn.send_command("terminal monitor")
            conn.send_command("debug ccsip messages")  # Enable debugs
            logger.info("debug ccsip messages enabled on CUBE.")
        return True

    except Exception as e:
        logger.error(f"Failed to enable debug on CUBE: {e}")
        return False


def disable_and_pull_logs(cube_config: dict = None) -> str:
    """
    SSH into the CUBE, disable SIP debug & pull the log buffer.
    Returns the raw log output as a string.
    """
    config = cube_config
    logger.info(
        f"Connecting to CUBE at {config['host']} to disable debug and pull logs..."
    )

    try:
        with ConnectHandler(**config) as conn:
            conn.enable()

            # Disable the debug
            conn.send_command("undebug all")
            logger.info("debug ccsip messages disabled on CUBE.")

            # Small pause to let the buffer settle
            time.sleep(1)

            # Pull the log buffer
            log_output = conn.send_command("show log", read_timeout=30)

        logger.info(f"Pulled {len(log_output)} characters of log output from CUBE.")
        return log_output

    except Exception as e:
        logger.error(f"Failed to pull logs from CUBE: {e}")
        return ""
