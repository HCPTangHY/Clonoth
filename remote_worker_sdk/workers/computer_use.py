"""Clonoth Remote Worker: Computer Use desktop control.

Run from remote_worker_sdk with:
    python workers/computer_use.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any

# [AutoC 2026-08-01] Load the local SDK without requiring pip install -e .
_SDK_ROOT = Path(__file__).resolve().parent.parent
if str(_SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SDK_ROOT))

# Load .env file from SDK root if it exists (no extra dependency needed).
_ENV_FILE = _SDK_ROOT / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        _key, _val = _key.strip(), _val.strip().strip('"').strip("'")
        if _key and _key not in os.environ:  # env var takes precedence over .env
            os.environ[_key] = _val

try:
    import mss
    import pyautogui
    import pyperclip
    from PIL import Image, ImageDraw
except ImportError as exc:
    raise SystemExit(
        "缺少依赖。请先执行：pip install websockets pyautogui mss pillow pyperclip"
    ) from exc

from clonoth_remote_worker import RemoteWorker

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("computer_use")

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.08


def _screen_size() -> tuple[int, int]:
    width, height = pyautogui.size()
    return int(width), int(height)


def _clamp_norm(value: Any) -> int:
    return max(0, min(1000, int(value)))


def _denorm_x(value: Any) -> int:
    # [AutoC 2026-08-01] Convert normalized 0-1000 coordinates to pixels at call time.
    # Why: display resolution can change after worker startup. How: read pyautogui.size
    # for every conversion and clamp the normalized input. Purpose: desktop actions
    # remain resolution-independent and stay on screen.
    width, _height = _screen_size()
    return round(_clamp_norm(value) / 1000 * max(1, width - 1))


def _denorm_y(value: Any) -> int:
    _width, height = _screen_size()
    return round(_clamp_norm(value) / 1000 * max(1, height - 1))


def _ok(result: str, **data: Any) -> dict[str, Any]:
    payload = {"result": result}
    payload.update(data)
    return {"ok": True, "data": payload}


def _fail(error: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "data": {"result": f"ERROR: {error}"}}


def _paste_text(text: str) -> None:
    # [AutoC 2026-08-01] Use clipboard paste for keyboard text entry.
    # Why: pyautogui.write/typewrite cannot reliably input Chinese or other Unicode
    # text. How: copy text with pyperclip and paste with Ctrl+V. Purpose: computer-use
    # typing works for Chinese prompts and multilingual UI text.
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")


def _clear_current_text() -> None:
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("delete")


def _mouse_info() -> dict[str, Any]:
    """Return current mouse position in pixels and normalized 0-1000."""
    mx, my = pyautogui.position()
    w, h = _screen_size()
    return {
        "mouse_x": int(mx), "mouse_y": int(my),
        "mouse_norm_x": round(mx / max(1, w - 1) * 1000),
        "mouse_norm_y": round(my / max(1, h - 1) * 1000),
        "screen_w": w, "screen_h": h,
    }


def _screenshot_attachment(mark_cursor: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture screen, optionally mark cursor with red dot. Returns (attachment, mouse_info)."""
    mouse = _mouse_info()
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        raw = sct.grab(monitor)
        image = Image.frombytes("RGB", raw.size, raw.rgb)
    if mark_cursor:
        draw = ImageDraw.Draw(image)
        cx, cy = mouse["mouse_x"], mouse["mouse_y"]
        r = 8
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="red", outline="white", width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    att = {
        "name": "screenshot.png",
        "mime_type": "image/png",
        "base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }
    return att, mouse


async def _action_result(text: str, delay: float = 1.0, **extra: Any) -> dict[str, Any]:
    """Standard action result: wait for UI response, then screenshot + mouse position."""
    if delay > 0:
        await asyncio.sleep(delay)
    att, mouse = _screenshot_attachment()
    data = {"result": text}
    data.update(mouse)
    data.update(extra)
    return {"ok": True, "data": data, "attachments": [att]}


worker = RemoteWorker(
    worker_id=os.environ.get("CLONOTH_WORKER_ID", f"cu-{platform.node()}"),
    namespace="computer",
    token=os.environ["CLONOTH_WORKER_TOKEN"],
    worker_name=os.environ.get("CLONOTH_WORKER_NAME", f"Computer Use ({platform.node()})"),
    max_concurrency=1,
    heartbeat_interval_sec=15,
    capabilities={"os": platform.system().lower(), "tags": ["gui", "computer_use"]},
)


@worker.tool(
    "screenshot",
    "Take a screenshot of the entire desktop. Returns a PNG image attachment.",
    {"type": "object", "properties": {"reasoning": {"type": "string"}}, "required": []},
    stateful=True,
    timeout_sec=10,
)
async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
    att, mouse = _screenshot_attachment()
    w, h = mouse["screen_w"], mouse["screen_h"]
    data = {"result": f"Screenshot captured ({w}x{h}). Cursor at pixel ({mouse['mouse_x']},{mouse['mouse_y']}), normalized ({mouse['mouse_norm_x']},{mouse['mouse_norm_y']})."}
    data.update(mouse)
    return {"ok": True, "data": data, "attachments": [att]}


@worker.tool(
    "mouse_click",
    "Click at normalized coordinates. Coordinates use 0-1000 where 0 is top/left and 1000 is bottom/right.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Normalized X coordinate, 0-1000."},
            "y": {"type": "integer", "description": "Normalized Y coordinate, 0-1000."},
            "button": {"type": "string", "enum": ["left", "middle", "right"]},
            "duration": {"type": "number", "description": "Optional mouse-down duration in seconds."},
            "reasoning": {"type": "string"},
        },
        "required": ["x", "y"],
    },
    stateful=True,
    timeout_sec=5,
)
async def mouse_click(args: dict[str, Any]) -> dict[str, Any]:
    x = _denorm_x(args["x"])
    y = _denorm_y(args["y"])
    button = str(args.get("button") or "left")
    duration = max(0.0, float(args.get("duration") or 0.0))
    if duration > 0:
        pyautogui.moveTo(x, y)
        pyautogui.mouseDown(button=button)
        await asyncio.sleep(duration)
        pyautogui.mouseUp(button=button)
    else:
        pyautogui.click(x=x, y=y, button=button)
    return await _action_result(f"Clicked at ({x}, {y}) with {button}.", x=x, y=y, button=button)


@worker.tool(
    "mouse_double_click",
    "Double-click at normalized coordinates.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "middle", "right"]},
            "reasoning": {"type": "string"},
        },
        "required": ["x", "y"],
    },
    stateful=True,
    timeout_sec=5,
)
async def mouse_double_click(args: dict[str, Any]) -> dict[str, Any]:
    x = _denorm_x(args["x"])
    y = _denorm_y(args["y"])
    button = str(args.get("button") or "left")
    pyautogui.doubleClick(x=x, y=y, button=button)
    return await _action_result(f"Double-clicked at ({x}, {y}) with {button}.", x=x, y=y, button=button)


@worker.tool(
    "mouse_hover",
    "Move the mouse to normalized coordinates and hover.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "duration": {"type": "number", "description": "Move duration in seconds. Default 0.2."},
            "reasoning": {"type": "string"},
        },
        "required": ["x", "y"],
    },
    stateful=True,
    timeout_sec=5,
)
async def mouse_hover(args: dict[str, Any]) -> dict[str, Any]:
    x = _denorm_x(args["x"])
    y = _denorm_y(args["y"])
    duration = max(0.0, float(args.get("duration") or 0.2))
    pyautogui.moveTo(x, y, duration=duration)
    return await _action_result(f"Hovered at ({x}, {y}).", delay=0.5, x=x, y=y)


@worker.tool(
    "mouse_drag",
    "Drag from start to end normalized coordinates.",
    {
        "type": "object",
        "properties": {
            "start_x": {"type": "integer"},
            "start_y": {"type": "integer"},
            "end_x": {"type": "integer"},
            "end_y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "middle", "right"]},
            "duration": {"type": "number", "description": "Drag duration in seconds. Default 0.5."},
            "reasoning": {"type": "string"},
        },
        "required": ["start_x", "start_y", "end_x", "end_y"],
    },
    stateful=True,
    timeout_sec=10,
)
async def mouse_drag(args: dict[str, Any]) -> dict[str, Any]:
    sx = _denorm_x(args["start_x"])
    sy = _denorm_y(args["start_y"])
    ex = _denorm_x(args["end_x"])
    ey = _denorm_y(args["end_y"])
    button = str(args.get("button") or "left")
    duration = max(0.0, float(args.get("duration") or 0.5))
    pyautogui.moveTo(sx, sy)
    pyautogui.dragTo(ex, ey, duration=duration, button=button)
    return await _action_result(f"Dragged from ({sx}, {sy}) to ({ex}, {ey}) with {button}.", start_x=sx, start_y=sy, end_x=ex, end_y=ey)


@worker.tool(
    "mouse_scroll",
    "Scroll the mouse wheel. Positive scroll_y scrolls down, negative scroll_y scrolls up.",
    {
        "type": "object",
        "properties": {
            "scroll_y": {"type": "integer", "description": "Vertical scroll amount. Positive means down."},
            "scroll_x": {"type": "integer", "description": "Horizontal scroll amount. Positive means right."},
            "reasoning": {"type": "string"},
        },
        "required": [],
    },
    stateful=True,
    timeout_sec=5,
)
async def mouse_scroll(args: dict[str, Any]) -> dict[str, Any]:
    scroll_y = int(args.get("scroll_y") or 0)
    scroll_x = int(args.get("scroll_x") or 0)
    if scroll_y:
        pyautogui.scroll(-scroll_y)
    if scroll_x and hasattr(pyautogui, "hscroll"):
        pyautogui.hscroll(scroll_x)
    return await _action_result(f"Scrolled x={scroll_x}, y={scroll_y}.", delay=0.5, scroll_x=scroll_x, scroll_y=scroll_y)


@worker.tool(
    "keyboard_type",
    "Type text into the focused field using clipboard paste for Unicode and Chinese support.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "clear_existing": {"type": "boolean", "description": "Clear existing text before typing."},
            "reasoning": {"type": "string"},
        },
        "required": ["text"],
    },
    stateful=True,
    timeout_sec=15,
)
async def keyboard_type(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text") or "")
    if args.get("clear_existing"):
        _clear_current_text()
    _paste_text(text)
    return await _action_result(f"Typed {len(text)} characters.", length=len(text))


@worker.tool(
    "keyboard_press",
    "Press one key or a key combination. Examples: ['enter'], ['ctrl', 'c'].",
    {
        "type": "object",
        "properties": {
            "keys": {"type": "array", "items": {"type": "string"}},
            "reasoning": {"type": "string"},
        },
        "required": ["keys"],
    },
    stateful=True,
    timeout_sec=5,
)
async def keyboard_press(args: dict[str, Any]) -> dict[str, Any]:
    keys = args.get("keys")
    if not isinstance(keys, list) or not keys:
        return _fail("keys must be a non-empty list")
    clean_keys = [str(key) for key in keys]
    if len(clean_keys) == 1:
        pyautogui.press(clean_keys[0])
    else:
        pyautogui.hotkey(*clean_keys)
    return await _action_result(f"Pressed {'+'.join(clean_keys)}.", keys=clean_keys)


@worker.tool(
    "clear_text",
    "Clear text in the focused input field with Ctrl+A and Delete.",
    {"type": "object", "properties": {"reasoning": {"type": "string"}}, "required": []},
    stateful=True,
    timeout_sec=5,
)
async def clear_text(args: dict[str, Any]) -> dict[str, Any]:
    _clear_current_text()
    return await _action_result("Text cleared.", delay=0.5)


@worker.tool(
    "click_and_type",
    "Click normalized coordinates, optionally clear existing text, then paste text.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "text": {"type": "string"},
            "clear_existing": {"type": "boolean", "description": "Default true."},
            "reasoning": {"type": "string"},
        },
        "required": ["x", "y"],
    },
    stateful=True,
    timeout_sec=15,
)
async def click_and_type(args: dict[str, Any]) -> dict[str, Any]:
    x = _denorm_x(args["x"])
    y = _denorm_y(args["y"])
    text = str(args.get("text") or "")
    clear_existing = bool(args.get("clear_existing", True))
    pyautogui.click(x=x, y=y)
    await asyncio.sleep(0.15)
    if clear_existing:
        _clear_current_text()
    if text:
        _paste_text(text)
    return await _action_result(f"Clicked ({x}, {y}) and typed {len(text)} characters.", x=x, y=y, length=len(text), cleared=clear_existing)


@worker.tool(
    "wait",
    "Wait for a specified number of seconds. Use for page loads and animations.",
    {
        "type": "object",
        "properties": {
            "seconds": {"type": "number", "description": "Wait time, clamped to 0-30 seconds."},
            "reasoning": {"type": "string"},
        },
        "required": ["seconds"],
    },
    stateful=True,
    timeout_sec=35,
)
async def wait_tool(args: dict[str, Any]) -> dict[str, Any]:
    seconds = max(0.0, min(30.0, float(args.get("seconds") or 0.0)))
    await asyncio.sleep(seconds)
    return await _action_result(f"Waited {seconds:g} seconds.", delay=0, seconds=seconds)


@worker.tool(
    "task_complete",
    "Signal that the current computer-use task is complete.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "success": {"type": "boolean"},
        },
        "required": ["summary", "success"],
    },
    stateful=True,
    timeout_sec=5,
)
async def task_complete(args: dict[str, Any]) -> dict[str, Any]:
    success = bool(args.get("success", True))
    summary = str(args.get("summary") or "")
    return {
        "ok": True,
        "data": {
            "result": f"Task {'completed' if success else 'failed'}: {summary}",
            "task_complete": True,
            "success": success,
            "summary": summary,
        },
    }


if __name__ == "__main__":
    supervisor_url = os.environ.get("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765")
    logger.info("Starting Computer Use worker. Supervisor: %s", supervisor_url)
    logger.info("Registered tools: %s", ", ".join(worker.tools.keys()))
    logger.info("pyautogui.FAILSAFE is enabled. Move mouse to a screen corner to abort pyautogui operations.")
    asyncio.run(worker.run(supervisor_url))
