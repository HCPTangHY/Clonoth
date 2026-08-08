"""Fallback Provider Plugin — auto-switch to backup API on primary failure.

Why: when the primary LLM provider exhausts retries (429/5xx), the task fails
with no recourse. How: hook into after_llm_call, detect non-OK responses for
retryable status codes, and replay the same request against fallback providers
listed in data/config.yaml. Purpose: improve availability without modifying
core llm_call.py or ai_step.py.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml

from .result import hook_result

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

PLUGIN_META = {
    "handler_class": "FallbackProviderHandler",
    "hook_points": [
        ("after_llm_call", "handle"),
    ],
    "priority": -20,  # run after error_snapshot (-10)
    "name": "fallback_provider",
    "description": "Primary API failure auto-fallback to backup providers",
}


def _load_fallbacks(workspace_root: str | Path) -> list[dict[str, Any]]:
    """Read top-level fallbacks list from data/config.yaml.

    Expected format:
        fallbacks:
          - provider: openai
            base_url: https://backup.com/v1
            api_key: sk-xxx
            model: optional-override
          - provider: openai
            base_url: https://backup2.com/v1
            api_key: sk-yyy
    """
    cfg_path = Path(workspace_root) / "data" / "config.yaml"
    if not cfg_path.exists():
        return []
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        fallbacks = cfg.get("fallbacks", [])
        if not isinstance(fallbacks, list):
            return []
        return fallbacks
    except Exception as exc:
        logger.warning("fallback_provider: failed to load config: %s", exc)
        return []


def _is_retryable(status_code: int | None) -> bool:
    """Check if the error is worth retrying on a different provider."""
    if status_code is None:
        return True  # unknown error, try fallback
    return status_code in _RETRYABLE_STATUS_CODES


def _create_fallback_provider(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 600.0,
) -> Any | None:
    """Instantiate a provider by type string using ProviderRegistry.

    Supports any provider registered in providers/__init__.py — no hardcoding.
    """
    try:
        from providers import registry
        provider_cls = registry.get(provider_type)
        if provider_cls is None:
            logger.warning("fallback_provider: unknown provider type '%s' (available: %s)",
                           provider_type, registry.list())
            return None
        # Build kwargs — different providers accept different params
        kwargs: dict[str, Any] = {"model": model, "api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if timeout:
            kwargs["timeout"] = timeout
        return provider_cls(**kwargs)
    except Exception as exc:
        logger.warning("fallback_provider: failed to create %s provider: %s", provider_type, exc)
        return None


class FallbackProviderHandler:
    """After-LLM-call hook: replay failed requests on fallback providers."""

    name = "fallback_provider"
    priority = -20

    async def handle(self, ctx: Any) -> Any | None:
        resp = ctx.response
        if resp is None or getattr(resp, "ok", True):
            return None  # success or no response, don't intervene

        status_code = getattr(resp, "status_code", None)
        if not _is_retryable(status_code):
            logger.debug(
                "fallback_provider: skipping non-retryable error (status=%s)",
                status_code,
            )
            return None

        # Get workspace root from rctx
        rctx = getattr(ctx, "rctx", None)
        workspace_root = getattr(rctx, "workspace_root", None) if rctx else None
        if not workspace_root:
            return None

        fallbacks = _load_fallbacks(workspace_root)
        if not fallbacks:
            return None

        original_error = getattr(resp, "error", "unknown")
        messages = ctx.messages
        tools = ctx.tools
        original_provider = ctx.provider
        original_model = getattr(original_provider, "model", "unknown")

        logger.warning(
            "fallback_provider: primary failed (status=%s error=%s model=%s), "
            "trying %d fallback(s)",
            status_code, original_error, original_model, len(fallbacks),
        )

        # Try each fallback
        for i, fb_cfg in enumerate(fallbacks):
            fb_provider_type = fb_cfg.get("provider", "openai").strip().lower()
            fb_base_url = fb_cfg.get("base_url", "").strip()
            fb_api_key = fb_cfg.get("api_key", "").strip()
            fb_model = fb_cfg.get("model", "").strip() or original_model

            if not fb_base_url or not fb_api_key:
                logger.warning(
                    "fallback_provider: skipping fallback[%d] — missing base_url or api_key",
                    i,
                )
                continue

            try:
                fb_provider = _create_fallback_provider(
                    provider_type=fb_provider_type,
                    base_url=fb_base_url,
                    api_key=fb_api_key,
                    model=fb_model,
                    timeout=getattr(original_provider, "timeout", 600.0),
                )
                if fb_provider is None:
                    logger.warning(
                        "fallback_provider: skipping fallback[%d] — unsupported provider type '%s'",
                        i, fb_provider_type,
                    )
                    continue

                logger.info(
                    "fallback_provider: trying fallback[%d] base_url=%s model=%s",
                    i, fb_base_url[:40], fb_model,
                )

                t0 = time.monotonic()
                new_resp = await fb_provider.chat(
                    messages=messages,
                    tools=tools,
                )
                elapsed = round((time.monotonic() - t0) * 1000, 1)

                if new_resp.ok:
                    logger.warning(
                        "fallback_provider: fallback[%d] succeeded in %.0fms "
                        "(base_url=%s model=%s)",
                        i, elapsed, fb_base_url[:40], fb_model,
                    )
                    # Overwrite ctx.response so ai_step uses the new response
                    ctx.response = new_resp

                    # Emit signal if bus is available
                    try:
                        from engine.signals import get_bus, Signal
                        bus = get_bus()
                        bus.emit(Signal(
                            name="llm.fallback",
                            payload={
                                "original_error": original_error,
                                "original_status": status_code,
                                "fallback_index": i,
                                "fallback_url": fb_base_url[:60],
                                "fallback_model": fb_model,
                                "success": True,
                                "elapsed_ms": elapsed,
                            },
                        ))
                    except Exception:
                        pass  # signal emission is best-effort

                    return None  # ai_step continues with updated ctx.response
                else:
                    logger.warning(
                        "fallback_provider: fallback[%d] also failed "
                        "(status=%s error=%s)",
                        i,
                        getattr(new_resp, "status_code", "?"),
                        getattr(new_resp, "error", "unknown"),
                    )

            except Exception as exc:
                logger.error(
                    "fallback_provider: fallback[%d] exception: %s",
                    i, exc, exc_info=True,
                )

        # All fallbacks failed, emit signal and let original error flow
        logger.error(
            "fallback_provider: all %d fallback(s) failed, "
            "original error stands (status=%s)",
            len(fallbacks), status_code,
        )
        try:
            from engine.signals import get_bus, Signal
            bus = get_bus()
            bus.emit(Signal(
                name="llm.fallback",
                payload={
                    "original_error": original_error,
                    "original_status": status_code,
                    "fallback_count": len(fallbacks),
                    "success": False,
                },
            ))
        except Exception:
            pass

        return None
