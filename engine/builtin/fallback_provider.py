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
# [fix 2026-05-28] Import message formatting utilities so fallback calls go
# through the same conversion pipeline as the primary call (llm_call.py L171-172).
# Without this, internal fields like _meta/_ephemeral leak into provider requests.
from engine.inference.llm_call import _build_messages_for_provider
from engine.attachments import prepare_messages_for_llm


def _strip_image_blocks(messages: list[dict]) -> list[dict]:
    """Remove image_url content blocks from all messages.

    Why: fallback providers like DeepSeek don't support vision/multimodal.
    How: for each message with list-type content, filter out image_url blocks.
    If a message becomes empty after stripping, replace with placeholder text.
    """
    result = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue
        filtered = [p for p in content if not (isinstance(p, dict) and p.get("type") == "image_url")]
        if not filtered:
            new_msg = {**msg, "content": "[image content removed]"}
        elif len(filtered) == 1 and isinstance(filtered[0], dict) and filtered[0].get("type") == "text":
            new_msg = {**msg, "content": filtered[0].get("text", "")}
        elif len(filtered) == len(content):
            result.append(msg)
            continue
        else:
            new_msg = {**msg, "content": filtered}
        result.append(new_msg)
    return result

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


def _load_config(workspace_root: str | Path) -> dict[str, Any]:
    """Read full data/config.yaml."""
    cfg_path = Path(workspace_root) / "data" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("fallback_provider: failed to load config: %s", exc)
        return {}


def _resolve_fallback_entry(fb_cfg: dict[str, Any], full_cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve a fallback entry by merging with its provider config block.

    Expected config layout:
        deepseek:
          base_url: https://api.deepseek.com
          api_key: sk-xxx
          model: deepseek-v4-pro
        fallbacks:
          - provider: deepseek          # just reference, auto-inherits from deepseek: block
          - provider: openai            # inherits from openai: block
            model: claude-sonnet-4-6    # override model only

    Resolution order for each field (base_url, api_key, model):
      1. Explicit value in fallback entry
      2. Value from the provider config block (e.g. deepseek:)
    """
    provider_name = (fb_cfg.get("provider") or "openai").strip().lower()
    provider_block = full_cfg.get(provider_name, {})
    if not isinstance(provider_block, dict):
        provider_block = {}

    return {
        "provider": provider_name,
        "base_url": (fb_cfg.get("base_url") or "").strip() or (provider_block.get("base_url") or "").strip(),
        "api_key": (fb_cfg.get("api_key") or "").strip() or (provider_block.get("api_key") or "").strip(),
        "model": (fb_cfg.get("model") or "").strip() or (provider_block.get("model") or "").strip(),
    }


def _is_retryable(status_code: int | None, error: str | None = None) -> bool:
    """Check if the error is worth retrying on a different provider.

    Covers both HTTP-level errors (429/5xx) and upstream provider errors
    that arrive as ok=False with HTTP 200 (e.g. content_filter, safety
    blocks, rate limits, empty responses via SSE error objects).
    """
    # Any response marked ok=False that reaches us is worth retrying,
    # unless it's a definitive client error (4xx other than 429).
    if status_code is None:
        return True  # unknown / connection-level error
    if status_code in _RETRYABLE_STATUS_CODES:
        return True
    # HTTP 200 but ok=False → upstream signaled error through body/SSE
    # (content_filter, safety block, quota via SSE error object, etc.)
    if status_code == 200 and error:
        return True
    # 4xx (except 429 and certain content moderation 403s) are client errors — don't retry
    # 403 can be either a true auth rejection or a content moderation refusal (safeguards).
    # Content moderation refusals are worth retrying on a different provider (e.g., Claude
    # cyber safeguard → fallback to DeepSeek); auth rejections are not.
    _CONTENT_MODERATION_KEYWORDS = {
        'safeguard', 'content_filter', 'refused', 'safety', 'cyber',
        'not allowed', 'community guidelines', 'moderation',
    }
    if status_code == 403 and error:
        err_lower = error.lower()
        if any(kw in err_lower for kw in _CONTENT_MODERATION_KEYWORDS):
            return True  # content moderation → retryable on different provider
        return False  # auth/permissions rejection → don't retry
    if 400 <= status_code < 500:
        return False
    return True  # anything else (1xx, 3xx, unknown) → try fallback


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
        original_error = getattr(resp, "error", None) or ""
        if not _is_retryable(status_code, original_error):
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

        full_cfg = _load_config(workspace_root)
        fallbacks_raw = full_cfg.get("fallbacks", [])
        if not isinstance(fallbacks_raw, list) or not fallbacks_raw:
            return None

        original_error = getattr(resp, "error", "unknown")
        messages = ctx.messages
        tools = ctx.tools
        original_provider = ctx.provider
        original_model = getattr(original_provider, "model", "unknown")

        # [fix 2026-05-28] Retrieve formatter from loop_state so we can run the
        # same message format conversion that llm_call.py does before provider.chat().
        # Why: without this, messages with _meta/_ephemeral fields are sent raw to
        # the fallback provider, causing "missing field type" errors on DS etc.
        _ls = ctx.extra.get("loop_state")
        _formatter = getattr(_ls, 'formatter', None) if _ls else None

        logger.warning(
            "fallback_provider: primary failed (status=%s error=%s model=%s), "
            "trying %d fallback(s)",
            status_code, original_error, original_model, len(fallbacks_raw),
        )

        # [fix 2026-05-28] Collect per-fallback error details so that when all
        # fallbacks fail, the user sees what was attempted and why each failed,
        # instead of only seeing the original provider's error message.
        fallback_errors: list[str] = []

        # Try each fallback in chain order
        for i, fb_raw in enumerate(fallbacks_raw):
            if not isinstance(fb_raw, dict):
                continue
            fb_cfg = _resolve_fallback_entry(fb_raw, full_cfg)
            fb_provider_type = fb_cfg["provider"]
            fb_base_url = fb_cfg["base_url"]
            fb_api_key = fb_cfg["api_key"]
            fb_model = fb_cfg["model"] or original_model

            if not fb_base_url or not fb_api_key:
                logger.warning(
                    "fallback_provider: skipping fallback[%d] (%s) — no base_url/api_key after resolve",
                    i, fb_provider_type,
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

                # [fix 2026-05-28] Format messages for this specific fallback
                # provider before calling chat(). Mirrors llm_call.py L171-172:
                #   1. _build_messages_for_provider: L2 formatter / bypass based
                #      on provider type (e.g. Responses/Gemini skip L2)
                #   2. prepare_messages_for_llm: resolve file:// image refs → base64
                # The third arg MUST be fb_provider (not original_provider) because
                # different providers need different format conversion paths.
                _fb_formatted = _build_messages_for_provider(
                    messages, _formatter, fb_provider,
                )
                _fb_messages = prepare_messages_for_llm(
                    _fb_formatted, workspace_root,
                ) if workspace_root else _fb_formatted

                # [fix 2026-05-28] Strip image_url blocks for non-vision
                # fallback providers (e.g. DeepSeek). Without this, messages
                # containing multimodal content cause deserialization errors
                # like: unknown variant `image_url`, expected `text`.
                _fb_messages = _strip_image_blocks(_fb_messages)

                t0 = time.monotonic()
                new_resp = await fb_provider.chat(
                    messages=_fb_messages,
                    tools=tools,
                )
                elapsed = round((time.monotonic() - t0) * 1000, 1)

                if new_resp.ok:
                    import sys; print(f"[FB-DIAG] fallback[{i}] SUCCEEDED in {elapsed:.0f}ms model={fb_model}", file=sys.stderr, flush=True)
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
                    _fb_err = getattr(new_resp, 'error', 'unknown')
                    _fb_st = getattr(new_resp, 'status_code', '?')
                    import sys; print(f"[FB-DIAG] fallback[{i}] ALSO FAILED status={_fb_st} error={str(_fb_err)[:200]}", file=sys.stderr, flush=True)
                    logger.warning(
                        "fallback_provider: fallback[%d] also failed "
                        "(status=%s error=%s)",
                        i, _fb_st, _fb_err,
                    )
                    # [fix 2026-05-28] Record this fallback's failure for later
                    # aggregation into the user-facing error message.
                    fallback_errors.append(
                        f"[Fallback {fb_provider_type} failed: {str(_fb_err)[:150]}]"
                    )

            except Exception as exc:
                import sys, traceback; print(f"[FB-DIAG] fallback[{i}] EXCEPTION: {exc}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
                logger.error(
                    "fallback_provider: fallback[%d] exception: %s",
                    i, exc, exc_info=True,
                )
                # [fix 2026-05-28] Record exception-type failures too.
                fallback_errors.append(
                    f"[Fallback {fb_cfg.get('provider', '?')} exception: {str(exc)[:150]}]"
                )

        # [fix 2026-05-28] Why: when all fallbacks fail, the user only sees the
        # original provider's error and has no idea fallbacks were even attempted.
        # How: append each fallback's failure reason to ctx.response.error.
        # Purpose: give the user full visibility into what was tried.
        if fallback_errors and hasattr(ctx.response, "error"):
            fb_summary = " | ".join(fallback_errors)
            ctx.response.error = f"{original_error} | {fb_summary}"

        # All fallbacks failed, emit signal and let original error flow
        logger.error(
            "fallback_provider: all %d fallback(s) failed, "
            "original error stands (status=%s)",
            len(fallbacks_raw), status_code,
        )
        try:
            from engine.signals import get_bus, Signal
            bus = get_bus()
            bus.emit(Signal(
                name="llm.fallback",
                payload={
                    "original_error": original_error,
                    "original_status": status_code,
                    "fallback_count": len(fallbacks_raw),
                    "success": False,
                },
            ))
        except Exception:
            pass

        return None
