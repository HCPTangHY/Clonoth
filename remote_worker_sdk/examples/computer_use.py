"""Clonoth Remote Worker — Computer Use (Desktop Control)

Based on HCPTangHY/gemini-computer-control tool definitions.
Uses 0-1000 normalized coordinate system for resolution independence.

Requirements:
    pip install websockets pyautogui mss pillow pyperclip

Usage:
    CLONOTH_WORKER_TOKEN=xxx python computer_use.py
    CLONOTH_WORKER_TOKEN=xxx CLONOTH_SUPERVISOR_URL=http://host:8765 python computer_use.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import platform
import time
from typing import Any

try:
    import mss
    import pyautogui
    import pyperclip
    from PIL import Image
except ImportError as exc:
    raise SystemExit(
        "computer_use.py requires: pyautogui mss pillow pyperclip\n"
        "Install: pip install pyautogui mss pillow pyperclip"
    ) from exc

from clonoth_remote_worker import RemoteWorker

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("computer_use")

# --- pyautogui config ---
pyautogui.FAILSAFE = True   # move mouse to corner to abort
pyautogui.PAUSE = 0.1

# --- Screen dimensions for coordinate denormalization ---
_screen_width, _screen_height = pyautogui.size()
logger.info("Screen size: %dx%d", _screen_width, _screen_height)


def _denorm_x(x: int) -> int:
    """Denormalize 0-1000 X coordinate to screen pixels."""
    return int(x / 1000 * _screen_width)


def _denorm_y(y: int) -> int:
    """Denormalize 0-1000 Y coordinate to screen pixels."""
    return int(y / 1000 * _screen_height)


def _take_screenshot(quality: int = 70, max_width: int = 1920) -> dict[str, Any]:
    """Capture screen, compress, return as base64 attachment."""
    with mss.mss() as sct:
        monitor = sct.monitors[0]  # entire virtual screen
        img = sct.grab(monitor)
        pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")

    # resize if too large
    if pil_img.width > max_width:
        ratio = max_width / pil_img.width
        pil_img = pil_img.resize(
            (max_width, int(pil_img.height * ratio)),
            Image.LANCZOS,
        )

    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    return {
        "name": "screenshot.jpg",
        "mime_type": "image/jpeg",
        "base64": b64,
    }


# --- Worker setup ---
worker = RemoteWorker(
    worker_id=os.environ.get("CLONOTH_WORKER_ID", f"cu-{platform.node()}"),
    namespace="computer",
    token=os.environ["CLONOTH_WORKER_TOKEN"],
    worker_name=os.environ.get("CLONOTH_WORKER_NAME", f"Computer Use ({platform.node()})"),
    max_concurrency=1,
    heartbeat_interval_sec=15,
    capabilities={"os": platform.system().lower(), "tags": ["gui", "computer_use"]},
)


# ============================================================================
# Tool: screenshot
# ============================================================================
@worker.tool(
    "screenshot",
    "Take a screenshot of the entire desktop. Returns a JPEG image attachment.",
    {
        "type": "object",
        "properties": {
            "quality": {
                "type": "integer",
                "description": "JPEG quality 1-100. Default 70.",
            },
        },
        "required": [],
    },
    stateful=True,
    timeout_sec=10,
)
async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
    quality = min(100, max(1, int(args.get("quality", 70))))
    attachment = _take_screenshot(quality=quality)
    return {
        "ok": True,
        "data": {"result": f"Screenshot captured ({_screen_width}x{_screen_height})"},
        "attachments": [attachment],
    }


# ============================================================================
# Tool: mouse_click
# ============================================================================
@worker.tool(
    "mouse_click",
    "Click at normalized coordinates (0-1000). Supports left/middle/right button and optional long-press duration.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Normalized X (0-1000). 0=left edge, 1000=right edge."},
            "y": {"type": "integer", "description": "Normalized Y (0-1000). 0=top edge, 1000=bottom edge."},
            "button": {"type": "string", "enum": ["left", "middle", "right"], "description": "Mouse button. Default: left."},
            "duration": {"type": "integer", "description": "Hold duration in ms. 0=normal click. Default: 0."},
            "reasoning": {"type": "string", "description": "Why this click."},
        },
        "required": ["x", "y"],
    },
    stateful=True,
    timeout_sec=5,
)
async def mouse_click(args: dict[str, Any]) -> dict[str, Any]:
    px = _denorm_x(int(args["x"]))
    py = _denorm_y(int(args["y"]))
    button = str(args.get("button", "left"))
    duration = int(args.get("duration", 0))
    if duration > 0:
        pyautogui.click(px, py, button=button, duration=duration / 1000)
    else:
        pyautogui.click(px, py, button=button)
    return {"ok": True, "data": {"result": f"Clicked ({px},{py}) {button}"}}


# ============================================================================
# Tool: mouse_double_click
# ============================================================================
@worker.tool(
    "mouse_double_click",
    "Double-click at normalized coordinates.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Normalized X (0-1000)."},
            "y": {"type": "integer", "description": "Normalized Y (0-1000)."},
            "button": {"type": "string", "enum": ["left", "middle", "right"]},
            "reasoning": {"type": "string"},
        },
        "required": ["x", "y"],
    },
    stateful=True,
    timeout_sec=5,
)
async def mouse_double_click(args: dict[str, Any]) -> dict[str, Any]:
    px = _denorm_x(int(args["x"]))
    py = _denorm_y(int(args["y"]))
    button = str(args.get("button", "left"))
    pyautogui.doubleClick(px, py, button=button)
    return {"ok": True, "data": {"result": f"Double-clicked ({px},{py}) {button}"}}


# ============================================================================
# Tool: mouse_hover
# ============================================================================
@worker.tool(
    "mouse_hover",
    "Move mouse to normalized coordinates and hover. Useful for triggering tooltips or dropdown menus.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Normalized X (0-1000)."},
            "y": {"type": "integer", "description": "Normalized Y (0-1000)."},
            "reasoning": {"type": "string"},
        },
        "required": ["x", "y"],
    },
    stateful=True,
    timeout_sec=5,
)
async def mouse_hover(args: dict[str, Any]) -> dict[str, Any]:
    px = _denorm_x(int(args["x"]))
    py = _denorm_y(int(args["y"]))
    pyautogui.moveTo(px, py, duration=0.3)
    return {"ok": True, "data": {"result": f"Hovered at ({px},{py})"}}


# ============================================================================
# Tool: mouse_drag
# ============================================================================
@worker.tool(
    "mouse_drag",
    "Drag from start to end coordinates (normalized 0-1000).",
    {
        "type": "object",
        "properties": {
            "start_x": {"type": "integer", "description": "Start X (0-1000)."},
            "start_y": {"type": "integer", "description": "Start Y (0-1000)."},
            "end_x": {"type": "integer", "description": "End X (0-1000)."},
            "end_y": {"type": "integer", "description": "End Y (0-1000)."},
            "button": {"type": "string", "enum": ["left", "middle", "right"]},
            "reasoning": {"type": "string"},
        },
        "required": ["start_x", "start_y", "end_x", "end_y"],
    },
    stateful=True,
    timeout_sec=10,
)
async def mouse_drag(args: dict[str, Any]) -> dict[str, Any]:
    sx = _denorm_x(int(args["start_x"]))
    sy = _denorm_y(int(args["start_y"]))
    ex = _denorm_x(int(args["end_x"]))
    ey = _denorm_y(int(args["end_y"]))
    button = str(args.get("button", "left"))
    pyautogui.moveTo(sx, sy)
    pyautogui.dragTo(ex, ey, button=button, duration=0.5)
    return {"ok": True, "data": {"result": f"Dragged ({sx},{sy})->({ex},{ey}) {button}"}}


# ============================================================================
# Tool: mouse_scroll
# ============================================================================
@worker.tool(
    "mouse_scroll",
    "Scroll mouse wheel. Positive scroll_y = down, negative = up. Positive scroll_x = right, negative = left.",
    {
        "type": "object",
        "properties": {
            "scroll_x": {"type": "integer", "description": "Horizontal scroll. + right, - left, 0 = none."},
            "scroll_y": {"type": "integer", "description": "Vertical scroll. + down, - up, 0 = none."},
            "reasoning": {"type": "string"},
        },
        "required": ["scroll_x", "scroll_y"],
    },
    stateful=True,
    timeout_sec=5,
)
async def mouse_scroll(args: dict[str, Any]) -> dict[str, Any]:
    sx = int(args.get("scroll_x", 0))
    sy = int(args.get("scroll_y", 0))
    if sy != 0:
        pyautogui.scroll(-sy)  # pyautogui: positive = up, we want positive = down
    if sx != 0:
        pyautogui.hscroll(sx)
    return {"ok": True, "data": {"result": f"Scrolled ({sx},{sy})"}}


# ============================================================================
# Tool: keyboard_type
# ============================================================================
@worker.tool(
    "keyboard_type",
    "Type text. Uses clipboard paste (Ctrl+V) for full Unicode/CJK support.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to type."},
            "clear_existing": {"type": "boolean", "description": "Ctrl+A Delete before typing. Default: false."},
            "reasoning": {"type": "string"},
        },
        "required": ["text"],
    },
    stateful=True,
    timeout_sec=10,
)
async def keyboard_type(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args["text"])
    if args.get("clear_existing"):
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")
    pyperclip.copy(text)
    pyautogui.hotkey("ctrl", "v")
    return {"ok": True, "data": {"result": f"Typed {len(text)} chars"}}


# ============================================================================
# Tool: keyboard_press
# ============================================================================
@worker.tool(
    "keyboard_press",
    "Press key(s). Single key: ['enter']. Combo: ['ctrl','c']. Supports: enter, esc, tab, space, backspace, delete, ctrl, shift, alt, win, up, down, left, right, home, end, pageup, pagedown, f1-f12.",
    {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key(s) to press. Single: ['enter']. Combo: ['ctrl','c'].",
            },
            "reasoning": {"type": "string"},
        },
        "required": ["keys"],
    },
    stateful=True,
    timeout_sec=5,
)
async def keyboard_press(args: dict[str, Any]) -> dict[str, Any]:
    keys = args["keys"]
    if not isinstance(keys, list) or not keys:
        return {"ok": False, "data": {"result": "ERROR: keys must be non-empty list"}, "error": "keys must be non-empty list"}
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)
    return {"ok": True, "data": {"result": f"Pressed {'+'.join(keys)}"}}


# ============================================================================
# Tool: clear_text
# ============================================================================
@worker.tool(
    "clear_text",
    "Clear text in current input field (Ctrl+A then Delete).",
    {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string"},
        },
        "required": [],
    },
    stateful=True,
    timeout_sec=5,
)
async def clear_text(args: dict[str, Any]) -> dict[str, Any]:
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("delete")
    return {"ok": True, "data": {"result": "Text cleared"}}


# ============================================================================
# Tool: click_and_type
# ============================================================================
@worker.tool(
    "click_and_type",
    "Click at normalized coordinates, optionally clear existing text, then type. Coordinates use 0-1000 system.",
    {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "Normalized X (0-1000)."},
            "y": {"type": "integer", "description": "Normalized Y (0-1000)."},
            "text": {"type": "string", "description": "Text to type. Empty = just click+clear."},
            "clear_existing": {"type": "boolean", "description": "Clear before typing. Default: true."},
            "reasoning": {"type": "string"},
        },
        "required": ["x", "y"],
    },
    stateful=True,
    timeout_sec=10,
)
async def click_and_type(args: dict[str, Any]) -> dict[str, Any]:
    px = _denorm_x(int(args["x"]))
    py = _denorm_y(int(args["y"]))
    text = str(args.get("text", ""))
    clear = args.get("clear_existing", True)
    pyautogui.click(px, py)
    await asyncio.sleep(0.2)
    if clear:
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")
    if text:
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
    parts = []
    if clear:
        parts.append("cleared")
    if text:
        parts.append(f"typed {len(text)} chars")
    return {"ok": True, "data": {"result": f"Click ({px},{py}), {', '.join(parts) or 'click only'}"}}


# ============================================================================
# Tool: wait
# ============================================================================
@worker.tool(
    "wait",
    "Wait for a specified duration (1-30 seconds). Use for page loads, animations, etc.",
    {
        "type": "object",
        "properties": {
            "seconds": {"type": "integer", "description": "Wait time in seconds (1-30)."},
            "reasoning": {"type": "string"},
        },
        "required": ["seconds"],
    },
    stateful=True,
    timeout_sec=35,
)
async def wait_tool(args: dict[str, Any]) -> dict[str, Any]:
    seconds = max(1, min(30, int(args.get("seconds", 1))))
    await asyncio.sleep(seconds)
    return {"ok": True, "data": {"result": f"Waited {seconds}s"}}


# ============================================================================
# Tool: task_complete
# ============================================================================
@worker.tool(
    "task_complete",
    "Signal that the current computer use task is complete. Provide a summary of what was done.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Summary of actions taken and results."},
            "success": {"type": "boolean", "description": "Whether the task succeeded."},
        },
        "required": ["summary", "success"],
    },
    stateful=True,
    timeout_sec=5,
)
async def task_complete(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "result": f"Task {'completed' if args.get('success', True) else 'failed'}: {args.get('summary', '')}",
            "task_complete": True,
            "success": bool(args.get("success", True)),
        },
    }


# ============================================================================
# Main
# ============================================================================
if __name__ == "__main__":
    url = os.environ.get("CLONOTH_SUPERVISOR_URL", "http://127.0.0.1:8765")
    logger.info("Starting Computer Use worker, connecting to %s", url)
    logger.info("Tools: %s", ", ".join(worker.tools.keys()))
    logger.info("Safety: move mouse to screen corner to abort (pyautogui FAILSAFE)")
    asyncio.run(worker.run(url))
