# run_bot.py
# PRODUCTION ENTRYPOINT
# FIX: was calling non-existent `run` function; now instantiates Controller correctly

import sys

from controller   import Controller
from logger_setup import get_logger

log = get_logger("run_bot")


def main():
    import config
    log.info("Starting bot in %s mode", config.MODE)

    # Override mode from CLI: python run_bot.py REAL
    if len(sys.argv) > 1 and sys.argv[1].upper() in ("PAPER", "REAL"):
        config.MODE = sys.argv[1].upper()
        log.info("Mode overridden from CLI → %s", config.MODE)

    bot = Controller(mode=config.MODE)
    bot.run_forever()


if __name__ == "__main__":
    main()
