"""
GitHub Pages state publisher — pushes bot_state.json via GitHub API.

Uses the GitHub Contents API (no git commands needed).
Works on Windows, Mac, Linux — any platform.

Requires GITHUB_TOKEN in .env (Personal Access Token with repo scope).
Create one at: https://github.com/settings/tokens
"""

import asyncio
import base64
import json
import logging
import os
import time

import httpx

import config

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(__file__), "bot_state.json")
PUSH_INTERVAL = 30  # seconds

# GitHub API config
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "Mikeytickets17/Kalshi")
GITHUB_BRANCH = "gh-pages"
GITHUB_PATH = "bot_state.json"
GITHUB_API = "https://api.github.com"


class GHPagesPublisher:
    """Publishes bot_state.json to gh-pages via GitHub API."""

    def __init__(self) -> None:
        self._running = False
        self._push_count = 0
        self._file_sha: str = ""  # SHA of existing file (needed for updates)
        self._http = httpx.AsyncClient(timeout=15.0)

    async def start(self) -> None:
        """Periodically push bot state to gh-pages branch."""
        self._running = True

        if not GITHUB_TOKEN:
            logger.warning(
                "GHPages publisher disabled — no GITHUB_TOKEN in .env. "
                "Create one at https://github.com/settings/tokens (repo scope)"
            )
            return

        logger.info("GHPages publisher started — pushing state every %ds", PUSH_INTERVAL)

        # Get existing file SHA (needed to update, not create)
        await self._get_existing_sha()

        # Wait for bot to generate initial state
        await asyncio.sleep(15)

        while self._running:
            try:
                success = await self._publish()
                if success:
                    self._push_count += 1
                    if self._push_count <= 3 or self._push_count % 20 == 0:
                        logger.info("GHPages: published state (push #%d)", self._push_count)
            except Exception as exc:
                logger.debug("GHPages publish error: %s", exc)
            await asyncio.sleep(PUSH_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()

    async def _get_existing_sha(self) -> None:
        """Get the SHA of the existing bot_state.json on gh-pages."""
        try:
            resp = await self._http.get(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}",
                params={"ref": GITHUB_BRANCH},
                headers=self._headers(),
            )
            if resp.status_code == 200:
                self._file_sha = resp.json().get("sha", "")
                logger.info("GHPages: found existing bot_state.json (sha=%s)", self._file_sha[:8])
            else:
                logger.info("GHPages: no existing bot_state.json, will create")
        except Exception as exc:
            logger.debug("GHPages: failed to get existing SHA: %s", exc)

    async def _publish(self) -> bool:
        """Read bot_state.json and push it to gh-pages via GitHub API."""
        if not os.path.exists(STATE_FILE):
            return False

        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError):
            return False

        # Add publish timestamp
        state["published_at"] = time.time()
        content = json.dumps(state)

        # Base64 encode for GitHub API
        content_b64 = base64.b64encode(content.encode()).decode()

        # Build request
        body = {
            "message": "Update bot state",
            "content": content_b64,
            "branch": GITHUB_BRANCH,
        }
        if self._file_sha:
            body["sha"] = self._file_sha

        try:
            resp = await self._http.put(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}",
                headers=self._headers(),
                json=body,
            )

            if resp.status_code in (200, 201):
                # Update SHA for next push
                data = resp.json()
                self._file_sha = data.get("content", {}).get("sha", self._file_sha)
                return True
            else:
                logger.debug("GHPages push failed: %d %s", resp.status_code, resp.text[:100])
                return False

        except Exception as exc:
            logger.debug("GHPages push error: %s", exc)
            return False

    def _headers(self) -> dict:
        return {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
