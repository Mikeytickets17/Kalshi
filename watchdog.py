"""
Huginn-style watchdog — keeps the bot alive 24/7.

Run this instead of bot.py directly. It:
1. Starts the bot
2. Monitors it for crashes
3. Auto-restarts on failure
4. Pulls latest code before restart
5. Logs everything

Usage:
    python watchdog.py

This is your "set it and forget it" launcher.
Never stops. Never sleeps. Always watching.
"""

import os
import subprocess
import sys
import time
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("watchdog.log", mode="a"),
    ],
)
logger = logging.getLogger("watchdog")

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BOT_DIR, "bot_state.json")
BOT_STDOUT_LOG = os.path.join(BOT_DIR, "bot_stdout.log")
MAX_RESTARTS = 1000
RESTART_DELAY = 10  # seconds between restarts
STALE_TIMEOUT = 300  # 5 minutes without update = stale


def current_branch() -> str:
    """Return the git branch the repo is currently on."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=BOT_DIR, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "HEAD"


def pull_latest(branch: str):
    """Pull latest code from git for the given branch."""
    try:
        result = subprocess.run(
            ["git", "pull", "origin", branch],
            cwd=BOT_DIR, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Git pull: %s", result.stdout.strip().split("\n")[-1])
        else:
            logger.warning("Git pull failed: %s", result.stderr.strip()[:100])
    except Exception as exc:
        logger.warning("Git pull error: %s", exc)


def check_state_fresh() -> bool:
    """Check if bot_state.json was updated recently."""
    try:
        if not os.path.exists(STATE_FILE):
            return False
        with open(STATE_FILE) as f:
            state = json.load(f)
        age = time.time() - state.get("last_updated", 0)
        return age < STALE_TIMEOUT
    except Exception:
        return False


def run_bot():
    """Run the bot as a subprocess.

    Route stdout/stderr to a log file instead of PIPE — an unread PIPE fills
    its OS buffer (~64KB on Windows), blocks the child on write, and kills
    the bot within seconds. Writing to a file means the bot can stream
    forever without back-pressure.

    The env tweak forces UTF-8 so Unicode arrows/emoji in log messages
    don't crash Python's default cp1252 stdout encoder on Windows.
    """
    logger.info("Starting bot...")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    stdout_f = open(BOT_STDOUT_LOG, "a", buffering=1, encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [sys.executable, "-u", "bot.py"],
        cwd=BOT_DIR,
        stdout=stdout_f,
        stderr=subprocess.STDOUT,
        env=env,
    )
    # Stash the file handle so we can close it on exit
    proc._stdout_file = stdout_f  # type: ignore[attr-defined]
    return proc


def main():
    branch = current_branch()
    logger.info("=" * 60)
    logger.info("WATCHDOG STARTED — monitoring Kalshi trading bot")
    logger.info("Bot directory: %s", BOT_DIR)
    logger.info("Branch: %s (auto-detected)", branch)
    logger.info("=" * 60)

    restart_count = 0

    while restart_count < MAX_RESTARTS:
        # Pull latest code before starting
        pull_latest(branch)

        # Start the bot
        proc = run_bot()
        start_time = time.time()
        logger.info("Bot started (PID %d, restart #%d)", proc.pid, restart_count)

        # Monitor the bot
        while True:
            # Check if process is still alive
            retcode = proc.poll()
            if retcode is not None:
                uptime = time.time() - start_time
                logger.warning(
                    "Bot exited with code %d after %.0f seconds",
                    retcode, uptime,
                )
                break

            # Check if state is being updated
            if time.time() - start_time > 60:  # Give it 60s to start
                if not check_state_fresh():
                    logger.warning("Bot state is stale — may be hung")

            time.sleep(10)

        # Close the stdout log handle so the file isn't left locked on Windows
        stdout_f = getattr(proc, "_stdout_file", None)
        if stdout_f:
            try:
                stdout_f.close()
            except Exception:
                pass

        # Bot stopped — restart after delay
        restart_count += 1
        logger.info("Restarting in %d seconds... (restart %d/%d)",
                     RESTART_DELAY, restart_count, MAX_RESTARTS)
        time.sleep(RESTART_DELAY)

    logger.error("Max restarts (%d) reached — stopping watchdog", MAX_RESTARTS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Watchdog stopped by user")
