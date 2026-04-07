"""
Multi-provider AI analyzer.

Tries every available AI provider in priority order for sentiment
analysis. Falls back automatically if one fails.

Priority:
  1. Anthropic Claude (best quality)
  2. Groq (free, fast, Llama 3.1 70B)
  3. Google Gemini (free tier)
  4. Ollama (local, no API key needed)
  5. OpenRouter (free models available)
  6. Rule-based fallback (always works)

Usage:
    ai = AIProvider()
    result = await ai.analyze("Trump announces Iran ceasefire")
    print(result)  # {"direction": "BULLISH", "confidence": 0.85, ...}
"""

import json
import logging
import time

import httpx

import config

logger = logging.getLogger(__name__)


class AIProvider:
    """Routes AI requests to the first available provider."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15.0)
        self._provider = self._detect_provider()
        logger.info("AI Provider: %s", self._provider)

    def _detect_provider(self) -> str:
        """Detect which AI provider is available."""
        if config.ANTHROPIC_API_KEY:
            return "anthropic"
        if config.GROQ_API_KEY:
            return "groq"
        if config.GEMINI_API_KEY:
            return "gemini"
        if config.OPENROUTER_API_KEY:
            return "openrouter"
        # Check if Ollama is potentially running (we'll verify on first call)
        if config.OLLAMA_URL:
            return "ollama"
        return "rules"

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def is_ai_enabled(self) -> bool:
        return self._provider != "rules"

    async def analyze(self, prompt: str) -> dict | None:
        """Send prompt to the best available AI and return parsed JSON response."""
        providers = ["anthropic", "groq", "gemini", "openrouter", "ollama"]

        # Start from current provider, try rest as fallbacks
        idx = providers.index(self._provider) if self._provider in providers else 0
        ordered = providers[idx:] + providers[:idx]

        for provider in ordered:
            try:
                result = await self._call_provider(provider, prompt)
                if result:
                    return result
            except Exception as exc:
                logger.debug("Provider %s failed: %s", provider, exc)
                continue

        return None

    async def _call_provider(self, provider: str, prompt: str) -> dict | None:
        """Call a specific AI provider."""
        if provider == "anthropic" and config.ANTHROPIC_API_KEY:
            return await self._call_anthropic(prompt)
        elif provider == "groq" and config.GROQ_API_KEY:
            return await self._call_groq(prompt)
        elif provider == "gemini" and config.GEMINI_API_KEY:
            return await self._call_gemini(prompt)
        elif provider == "openrouter" and config.OPENROUTER_API_KEY:
            return await self._call_openrouter(prompt)
        elif provider == "ollama":
            return await self._call_ollama(prompt)
        return None

    async def _call_anthropic(self, prompt: str) -> dict | None:
        """Anthropic Claude API."""
        resp = await self._http.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        return self._extract_json(text)

    async def _call_groq(self, prompt: str) -> dict | None:
        """Groq API — FREE tier, runs Llama 3.1 70B."""
        resp = await self._http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "temperature": 0.1,
            },
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return self._extract_json(text)

    async def _call_gemini(self, prompt: str) -> dict | None:
        """Google Gemini API — FREE tier, 15 req/min."""
        resp = await self._http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 300, "temperature": 0.1},
            },
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return self._extract_json(text)

    async def _call_ollama(self, prompt: str) -> dict | None:
        """Ollama — 100% FREE, runs locally."""
        try:
            resp = await self._http.post(
                f"{config.OLLAMA_URL}/api/generate",
                json={
                    "model": config.OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.1},
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            text = resp.json()["response"]
            return self._extract_json(text)
        except httpx.ConnectError:
            logger.debug("Ollama not running at %s", config.OLLAMA_URL)
            return None

    async def _call_openrouter(self, prompt: str) -> dict | None:
        """OpenRouter — some free models available."""
        resp = await self._http.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "temperature": 0.1,
            },
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
        return self._extract_json(text)

    def _extract_json(self, text: str) -> dict | None:
        """Extract JSON from AI response text."""
        text = text.strip()
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try extracting from markdown code block
        if "```" in text:
            start = text.find("```")
            end = text.find("```", start + 3)
            if end > start:
                block = text[start + 3:end].strip()
                if block.startswith("json"):
                    block = block[4:].strip()
                try:
                    return json.loads(block)
                except json.JSONDecodeError:
                    pass
        # Try finding JSON object in text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        logger.warning("Failed to extract JSON from AI response: %s", text[:200])
        return None

    async def close(self) -> None:
        await self._http.aclose()
