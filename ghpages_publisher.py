"""
GitHub Pages state publisher.

Pushes bot_state.json to the gh-pages branch every 30 seconds
so the GitHub Pages dashboard shows real bot data.

Uses git commands — requires the repo to already be cloned
with push access (which it is on the user's machine).
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(__file__), "bot_state.json")
PUSH_INTERVAL = 30  # seconds


class GHPagesPublisher:
    """Publishes bot_state.json to gh-pages branch for the dashboard."""

    def __init__(self, repo_dir: str = None) -> None:
        self._repo_dir = repo_dir or os.path.dirname(__file__)
        self._running = False
        self._push_count = 0

    async def start(self) -> None:
        """Periodically push bot state to gh-pages branch."""
        self._running = True
        logger.info("GHPages publisher started — pushing state every %ds", PUSH_INTERVAL)

        # Wait for bot to generate initial state
        await asyncio.sleep(10)

        while self._running:
            try:
                await self._publish()
            except Exception as exc:
                logger.debug("GHPages publish error: %s", exc)
            await asyncio.sleep(PUSH_INTERVAL)

    async def stop(self) -> None:
        self._running = False

    async def _publish(self) -> None:
        """Read bot_state.json and push it to gh-pages branch."""
        if not os.path.exists(STATE_FILE):
            return

        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError):
            return

        # Add timestamp
        state["published_at"] = time.time()
        state_json = json.dumps(state, indent=2)

        # Use git to push to gh-pages branch without switching branches
        # This creates/updates bot_state.json on gh-pages using git commands
        loop = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, self._git_push, state_json)

        if success:
            self._push_count += 1
            if self._push_count % 10 == 1:  # Log every 10th push to avoid spam
                logger.info("GHPages: published state (push #%d)", self._push_count)

    def _git_push(self, state_json: str) -> bool:
        """Push bot_state.json to gh-pages using git."""
        try:
            cwd = self._repo_dir

            # Write state to a temp file
            tmp_state = os.path.join(cwd, "_tmp_state.json")
            with open(tmp_state, "w") as f:
                f.write(state_json)

            # Use git hash-object + update-ref to push without checkout
            # Step 1: Hash the blob
            result = subprocess.run(
                ["git", "hash-object", "-w", tmp_state],
                capture_output=True, text=True, cwd=cwd,
            )
            if result.returncode != 0:
                return False
            blob_hash = result.stdout.strip()

            # Step 2: Get the current gh-pages tree
            result = subprocess.run(
                ["git", "rev-parse", "origin/gh-pages^{tree}"],
                capture_output=True, text=True, cwd=cwd,
            )
            if result.returncode != 0:
                return False
            tree_hash = result.stdout.strip()

            # Step 3: Create new tree with bot_state.json added/updated
            tree_entry = f"100644 blob {blob_hash}\tbot_state.json"
            # Read existing tree and add/replace bot_state.json
            result = subprocess.run(
                ["git", "ls-tree", tree_hash],
                capture_output=True, text=True, cwd=cwd,
            )
            lines = result.stdout.strip().split("\n")
            # Remove old bot_state.json if present
            lines = [l for l in lines if not l.endswith("\tbot_state.json")]
            lines.append(tree_entry)

            # Create new tree
            tree_input = "\n".join(lines) + "\n"
            result = subprocess.run(
                ["git", "mktree"],
                input=tree_input, capture_output=True, text=True, cwd=cwd,
            )
            if result.returncode != 0:
                return False
            new_tree = result.stdout.strip()

            # Step 4: Create commit
            parent = subprocess.run(
                ["git", "rev-parse", "origin/gh-pages"],
                capture_output=True, text=True, cwd=cwd,
            ).stdout.strip()

            result = subprocess.run(
                ["git", "commit-tree", new_tree, "-p", parent,
                 "-m", "Update bot state"],
                capture_output=True, text=True, cwd=cwd,
            )
            if result.returncode != 0:
                return False
            commit_hash = result.stdout.strip()

            # Step 5: Push
            result = subprocess.run(
                ["git", "push", "origin", f"{commit_hash}:refs/heads/gh-pages"],
                capture_output=True, text=True, cwd=cwd,
            )

            # Clean up
            try:
                os.remove(tmp_state)
            except OSError:
                pass

            return result.returncode == 0

        except Exception as exc:
            logger.debug("Git push error: %s", exc)
            return False
