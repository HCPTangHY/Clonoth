"""Clonoth external tool: Discord 服务器管理

通过本地 Bridge Server 在 Bot 进程内执行 discord.py 代码。
可以调用任意 discord.py API，自由度不受限制。
"""
import sys
import json
import urllib.request
import urllib.error
import os

SPEC = {
    "name": "discord_manage",
    "description": (
        "在 Discord Bot 进程内执行 Python 代码，可直接调用 discord.py 的全部 API。\n"
        "代码中可用的预置变量：\n"
        "  - client: discord.Client 实例（已登录）\n"
        "  - discord: discord 模块\n"
        "  - datetime: datetime 模块\n"
        "  - asyncio: asyncio 模块\n"
        "代码以 async 函数体的方式执行，可以直接使用 await。\n"
        "用 return 返回结果（dict/list/str/int 均可）。\n"
        "最大执行时间 60 秒。\n\n"
        "port 参数指定目标 Bot 的 Bridge Server 端口号。\n\n"
        "示例 - 查询服务器信息：\n"
        "  guilds = [{'id': g.id, 'name': g.name} for g in client.guilds]\n"
        "  return guilds"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的 Python 代码（async 函数体，可直接 await，用 return 返回结果）"
            },
            "port": {
                "type": "integer",
                "description": "目标 Bot 的 Bridge Server 端口号"
            }
        },
        "required": ["code", "port"]
    }
}

TIMEOUT_SEC = 65

BRIDGE_HOST = os.environ.get("DISCORD_BRIDGE_HOST", "127.0.0.1")


def output(result):
    # [AutoC 2026-05-31] Why: discord_manage returns arbitrary bridge-server JSON,
    # but the engine now expects ok/data/error with data.result. How: keep the
    # original bridge response under data.payload and derive a compact readable
    # summary. Purpose: preserve full Discord API output while making history text
    # uniform.
    if isinstance(result, dict) and isinstance(result.get("result"), str):
        result_text = result["result"]
    elif isinstance(result, dict) and isinstance(result.get("text"), str):
        result_text = result["text"]
    else:
        result_text = json.dumps(result, ensure_ascii=False, default=str)
    print(json.dumps({"ok": True, "data": {"result": result_text, "payload": result}}, ensure_ascii=False, default=str))
    sys.exit(0)


def fail(error):
    # [AutoC 2026-05-31] Why: bridge failures should include data.result like every
    # other external tool. How: add the readable ERROR string under data. Purpose:
    # keep validation and HTTP errors visible after schema migration.
    print(json.dumps({"ok": False, "error": str(error), "data": {"result": f"ERROR: {error}"}}, ensure_ascii=False))
    sys.exit(1)


if __name__ == "__main__":
    try:
        args = json.loads(sys.stdin.read())
    except Exception as e:
        fail(f"无法解析输入: {e}")

    code = args.get("code", "")
    if not code:
        fail("缺少 code 参数")

    port = args.get("port")
    if not port:
        fail("缺少 port 参数")

    url = f"http://{BRIDGE_HOST}:{port}/discord"
    payload = json.dumps({"code": code}, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            output(body)
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            fail(err_body.get("error", f"HTTP {e.code}"))
        except Exception:
            fail(f"HTTP {e.code}")
    except Exception as e:
        fail(str(e))
