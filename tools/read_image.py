from __future__ import annotations

"""Image understanding tool for text-only models.

Provides vision capabilities to non-multimodal models (like DeepSeek) by calling
a multimodal model (Gemini) via OpenAI-compatible API to describe image content.

ONLY for text-only models. Multimodal models should read images directly.

Requires GEMINI_API_KEY or OPENAI_API_KEY environment variable.
"""

SPEC = {
    "name": "read_image",
    "async_mode": False,
    "description": (
        "[ONLY for text-only models like DeepSeek. Do NOT use if you can see images natively.] "
        "Analyze image(s) and return a comprehensive text description including all visible text (OCR), "
        "layout, colors, objects, people, style, and context. "
        "Provide local image paths to analyze. Uses gemini-3-flash-preview internally."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of local image paths (relative to workspace root) to analyze.",
            },
            "image_path": {
                "type": "string",
                "description": "Single local image path to analyze (convenience shorthand).",
            },
            "focus": {
                "type": "string",
                "description": "Optional focus area: 'OCR', 'style', 'objects', 'layout', or free text. Default: comprehensive.",
            },
        },
        "required": [],
    },
}

TIMEOUT_SEC = 120

if __name__ == "__main__":
    import json
    import sys
    import os
    import base64
    from pathlib import Path
    from urllib import request as urllib_request
    from urllib.error import HTTPError, URLError

    _input = json.loads(sys.stdin.read())

    def output(result):
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0)

    def fail(error):
        # [AutoC 2026-05-31] Why: read_image may fail before it can return a
        # description, but the engine still expects data.result for readable tool
        # history. How: include ERROR text under data.result in the failure JSON.
        # Purpose: keep vision-tool failures understandable after schema migration.
        print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
        sys.exit(1)

    args = _input

    # ---- Collect image paths ----
    raw_paths = args.get("image_paths") or args.get("image_path") or []
    if isinstance(raw_paths, str):
        image_paths = [raw_paths]
    elif isinstance(raw_paths, list):
        image_paths = raw_paths
    else:
        image_paths = []

    if not image_paths:
        fail("No image_paths provided. Supply image_path or image_paths.")

    focus = str(args.get("focus") or "").strip()

    # ---- API Configuration ----
    def _load_dotenv():
        env_path = Path.cwd() / ".env"
        kv = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip().strip("'\"")
        return kv

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")

    if not api_key or not base_url:
        dotenv = _load_dotenv()
        if not api_key:
            api_key = dotenv.get("GEMINI_API_KEY", "") or dotenv.get("OPENAI_API_KEY", "")
        if not base_url:
            base_url = dotenv.get("OPENAI_BASE_URL", "").rstrip("/")

    if not api_key:
        fail("No API key found in env or .env file")
    if not base_url:
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    if not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1" if "/v1" not in base_url else base_url

    model = "gemini-3-flash-preview"
    url = f"{base_url}/chat/completions"

    # ---- Build content parts ----
    content_parts = []

    for img_path in image_paths:
        p = Path.cwd() / str(img_path).strip()
        if not p.exists():
            fail(f"Image not found: {img_path}")

        ext = p.suffix.lower()
        mime = "image/png"
        if ext in [".jpg", ".jpeg"]:
            mime = "image/jpeg"
        elif ext == ".webp":
            mime = "image/webp"
        elif ext == ".gif":
            mime = "image/gif"
        elif ext == ".bmp":
            mime = "image/bmp"

        try:
            b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
            content_parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{b64}"
                }
            })
        except Exception as e:
            fail(f"Failed to read image {img_path}: {e}")

    # ---- System & user prompt ----
    focus_instruction = ""
    if focus:
        focus_instruction = f"\nThe user specifically wants you to focus on: {focus}."

    system_prompt = (
        "You are an expert image analyst. Your job is to describe images in exhaustive detail "
        "so that someone who cannot see the image gets a complete understanding of its content.\n\n"
        "For EVERY image, you MUST cover ALL of the following:\n"
        "1. **OCR / Text**: Transcribe ALL visible text exactly as written — titles, labels, captions, "
        "watermarks, UI text, code snippets, error messages, chat bubbles, etc. Preserve formatting.\n"
        "2. **Layout & Composition**: Describe spatial arrangement — what is where, columns, rows, "
        "panels, split screens, overlays, margins, alignment.\n"
        "3. **Visual Style**: Art style, color palette, lighting, contrast, filters, resolution quality.\n"
        "4. **Objects & Entities**: Every distinct object, icon, logo, symbol, UI element, chart, graph.\n"
        "5. **People & Actions**: Faces, expressions, poses, gestures, clothing, interactions.\n"
        "6. **Context & Meaning**: What is this image about? Is it a screenshot, photo, diagram, meme, "
        "chart, code, conversation? What platform/app is shown? What is the overall message?\n\n"
        "Be thorough. Miss nothing. If the image contains code or terminal output, reproduce it verbatim.\n"
        "If there are multiple images, describe each one separately with clear numbering."
        + focus_instruction
    )

    content_parts.append({
        "type": "text",
        "text": "Describe this image in complete detail following your instructions."
    })

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_parts}
        ],
        "temperature": 0.1,
        "max_tokens": 4096
    }

    req_data = json.dumps(body).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        },
        method="POST",
    )

    # ---- Send request ----
    try:
        with urllib_request.urlopen(req, timeout=100) as resp:
            resp_json = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        fail(f"API HTTP {e.code}: {error_body}")
    except URLError as e:
        fail(f"API connection error: {e.reason}")
    except Exception as e:
        fail(f"API request failed: {e}")

    # ---- Parse response ----
    choices = resp_json.get("choices")
    if not isinstance(choices, list) or not choices:
        fail(f"No choices in response: {json.dumps(resp_json)[:500]}")

    message = choices[0].get("message", {})
    description = message.get("content", "").strip()

    if not description:
        fail(f"Empty response from vision model: {json.dumps(resp_json)[:500]}")

    # [AutoC 2026-05-31] Why: read_image's description is the human-readable tool
    # result and should live at data.result. How: move description and metadata
    # under data while keeping the same values. Purpose: conform to ok/data/error
    # without losing model and image count metadata.
    output({
        "ok": True,
        "data": {
            "result": description,
            "description": description,
            "model": model,
            "images_analyzed": len(image_paths)
        }
    })
