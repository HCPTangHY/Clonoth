"""Clonoth external tool: Discord 服务器管理

通过本地 Bridge Server 在 Bot 进程内执行 discord.py 代码。
可以调用任意 discord.py API，自由度不受限制。
"""
import sys
import json
import urllib.request
import urllib.error

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
        "示例 1 - 禁言用户 60 秒：\n"
        "  guild = client.get_guild(123456)\n"
        "  member = guild.get_member(789012)\n"
        "  until = discord.utils.utcnow() + datetime.timedelta(seconds=60)\n"
        "  await member.timeout(until, reason='违规发言')\n"
        "  return {'done': True}\n\n"
        "示例 2 - 封禁用户并在指定频道公示：\n"
        "  guild = client.get_guild(123456)\n"
        "  member = guild.get_member(789012)\n"
        "  await guild.ban(member, reason='严重违规')\n"
        "  ch = client.get_channel(111222)\n"
        "  await ch.send(f'{member.display_name} 已被封禁，原因：严重违规')\n"
        "  return {'banned': True, 'announced': True}\n\n"
        "示例 3 - 查询服务器信息：\n"
        "  guilds = [{'id': g.id, 'name': g.name} for g in client.guilds]\n"
        "  return guilds"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的 Python 代码（async 函数体，可直接 await，用 return 返回结果）"
            }
        },
        "required": ["code"]
    }
}

TIMEOUT_SEC = 65

BRIDGE_URL = "http://127.0.0.1:8766/discord"


def output(result):
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(0)


def fail(error):
    print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
    sys.exit(1)


if __name__ == "__main__":
    try:
        args = json.loads(sys.stdin.read())
    except Exception as e:
        fail(f"无法解析输入: {e}")

    code = args.get("code", "")
    if not code:
        fail("缺少 code 参数")

    payload = json.dumps({"code": code}, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        BRIDGE_URL,
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
