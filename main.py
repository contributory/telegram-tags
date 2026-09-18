import asyncio
import json
import logging
import os
import platform
import random
import sys
import time
from urllib.parse import unquote

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TAGS = [
    "Đuỵch Vợ Bạn",
    "Quên Chùi Đít",
    "Tè Bậy",
    "Đang Thèm Cặc",
    "Bị Thiểu Năng",
    "Tu Tiên Nứng Lồn",
    "Ăn Nhầm Cức",
]
TAGS_1 = ["Đang", "Vạn Kiếp", "Vừa", "Muốn", "Mê", "Cần", "Thích"]
TAGS_2 = [
    "Quay Tay",
    "Sóc Lọ",
    "Thẩm Du",
    "Buscu",
    "Bú Lồn",
    "Làm Đũy",
    "Đá Phò",
    "Tè Bậy",
    "Tè Dầm",
    "Ăn Cức",
]

_bot_user_id_cache: dict[str, int] = {}
_bot_commands_registered: set[str] = set()

RATE_LIMIT_PER_SEC = 25
BUCKET_CAPACITY = 25
USER_TAG_COOLDOWN_SECONDS = 60

_buckets: dict[str, "TokenBucket"] = {}
_user_tag_locks: dict[tuple[str, int, int], asyncio.Lock] = {}
_user_last_tag: dict[tuple[str, int, int], float] = {}


class TokenBucket:
    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(
                self.capacity, self.tokens + (now - self.updated) * self.rate
            )
            self.updated = now
            if self.tokens < 1:
                await asyncio.sleep((1 - self.tokens) / self.rate)
                self.tokens = 0.0
                self.updated = time.monotonic()
            else:
                self.tokens -= 1


def _get_bucket(bot_token: str) -> "TokenBucket":
    bucket = _buckets.get(bot_token)
    if bucket is None:
        bucket = TokenBucket(RATE_LIMIT_PER_SEC, BUCKET_CAPACITY)
        _buckets[bot_token] = bucket
    return bucket


async def telegram_request(bot_token: str, method: str, **params):
    await _get_bucket(bot_token).acquire()
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=params)
        return resp.json()


async def get_bot_user_id(bot_token: str) -> int:
    if bot_token not in _bot_user_id_cache:
        me = await telegram_request(bot_token, "getMe")
        _bot_user_id_cache[bot_token] = me["result"]["id"]
    return _bot_user_id_cache[bot_token]


async def is_bot_admin(bot_token: str, chat_id: int) -> bool:
    bot_user_id = await get_bot_user_id(bot_token)
    result = await telegram_request(
        bot_token, "getChatMember", chat_id=chat_id, user_id=bot_user_id
    )
    status = result.get("result", {}).get("status", "")
    return status in ("administrator", "creator")


async def is_user_admin(bot_token: str, chat_id: int, user_id: int) -> bool:
    result = await telegram_request(
        bot_token, "getChatMember", chat_id=chat_id, user_id=user_id
    )
    status = result.get("result", {}).get("status", "")
    return status in ("administrator", "creator")


async def set_user_tag(bot_token: str, chat_id: int, user_id: int, tag: str) -> dict:
    return await telegram_request(
        bot_token, "setChatMemberTag", chat_id=chat_id, user_id=user_id, tag=tag[:16]
    )


async def ensure_bot_commands(bot_token: str) -> None:
    if bot_token in _bot_commands_registered:
        return

    result = await telegram_request(
        bot_token,
        "setMyCommands",
        commands=[
            {
                "command": "checkwebhook",
                "description": "Kiểm tra trạng thái webhook",
            }
        ],
    )
    if result.get("ok"):
        _bot_commands_registered.add(bot_token)
    else:
        logger.warning("setMyCommands thất bại: %s", result.get("description"))


def _message_command(message: dict) -> str | None:
    text = message.get("text")
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0].split("@", 1)[0].lower()


async def handle_checkwebhook(bot_token: str, message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    if chat_id is None:
        return

    info = await telegram_request(bot_token, "getWebhookInfo")
    if not info.get("ok"):
        text = (
            "⚠️ Lệnh đã đến được bot qua webhook, nhưng không đọc được "
            f"getWebhookInfo: {info.get('description', 'Unknown error')}"
        )
    else:
        data = info.get("result", {})
        pending = data.get("pending_update_count", 0)
        max_connections = data.get("max_connections")
        last_error = data.get("last_error_message")

        lines = [
            "✅ Webhook đang hoạt động — lệnh này vừa được nhận qua webhook.",
            f"Pending updates: {pending}",
        ]
        if max_connections is not None:
            lines.append(f"Max connections: {max_connections}")
        if last_error:
            lines.append(f"Lỗi gần nhất: {last_error}")
        else:
            lines.append("Lỗi gần nhất: không có")
        text = "\n".join(lines)

    await telegram_request(bot_token, "sendMessage", chat_id=chat_id, text=text)


async def _json_response(send, data, status=200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _text_response(send, text, status=200):
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive):
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


def _base_url(scope):
    headers = {k.lower(): v for k, v in scope.get("headers", [])}
    host = headers.get(b"x-forwarded-host") or headers.get(b"host") or b"localhost"
    proto = headers.get(b"x-forwarded-proto")
    scheme = proto.decode() if proto else scope.get("scheme", "https")
    return f"{scheme}://{host.decode()}"


async def _handle_webhook(bot_token: str, receive, send):
    raw = await _read_body(receive)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        await _json_response(send, {"detail": "Invalid JSON"}, 400)
        return

    if "message" not in body:
        await _json_response(send, {"ok": True})
        return

    message = body["message"]
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")

    try:
        await ensure_bot_commands(bot_token)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
        logger.warning("Không thể đăng ký bot commands: %s", e)

    if _message_command(message) == "/checkwebhook":
        try:
            await handle_checkwebhook(bot_token, message)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
            logger.error("Lỗi khi kiểm tra webhook: %s", e)
        await _json_response(send, {"ok": True})
        return

    if chat_type not in ("group", "supergroup"):
        await _json_response(send, {"ok": True})
        return

    from_user = message.get("from", {})
    user_id = from_user.get("id")

    bot_user_id = await get_bot_user_id(bot_token)
    if user_id == bot_user_id:
        await _json_response(send, {"ok": True})
        return

    if not await is_bot_admin(bot_token, chat_id):
        logger.info("Bot không phải admin ở nhóm %s, bỏ qua.", chat_id)
        await _json_response(send, {"ok": True})
        return

    if await is_user_admin(bot_token, chat_id, user_id):
        logger.info("Người dùng %s là admin, không đổi nhãn.", user_id)
        await _json_response(send, {"ok": True})
        return

    key = (bot_token, chat_id, user_id)
    if time.monotonic() - _user_last_tag.get(key, 0.0) < USER_TAG_COOLDOWN_SECONDS:
        await _json_response(send, {"ok": True})
        return

    lock = _user_tag_locks.setdefault(key, asyncio.Lock())
    async with lock:
        if time.monotonic() - _user_last_tag.get(key, 0.0) < USER_TAG_COOLDOWN_SECONDS:
            await _json_response(send, {"ok": True})
            return

        t1 = random.choice(TAGS_1)
        t2 = random.choice(TAGS_2)
        tfinal = f"{t1} {t2}"
        taio = random.choice(TAGS)
        new_tag = random.choice([tfinal, taio, tfinal])

        try:
            result = await set_user_tag(bot_token, chat_id, user_id, new_tag)
            if not result.get("ok"):
                logger.warning(
                    "setChatMemberTag thất bại: %s", result.get("description")
                )
            else:
                _user_last_tag[key] = time.monotonic()
                logger.info(
                    "Đã gắn nhãn cho người dùng %s thành '%s' trong nhóm %s",
                    user_id,
                    new_tag,
                    chat_id,
                )
        except httpx.HTTPError as e:
            logger.error("Lỗi khi gắn nhãn cho user %s: %s", user_id, e)

    await _json_response(send, {"ok": True})


async def app(scope, receive, send):
    if scope["type"] != "http":
        return

    method = scope.get("method", "GET")
    path = scope.get("path", "/")

    if method == "GET" and path == "/":
        await _json_response(send, {"status": "running", "bot": "Telegram Webhook Bot"})
        return

    if method == "GET" and path == "/os":
        uname = platform.uname()
        lines = [
            f"platform    : {platform.platform()}",
            f"system      : {uname.system}",
            f"node        : {uname.node}",
            f"release     : {uname.release}",
            f"version     : {uname.version}",
            f"machine     : {uname.machine}",
            f"processor   : {uname.processor}",
            f"python      : {sys.version}",
            f"executable  : {sys.executable}",
            f"cpu_count   : {os.cpu_count()}",
            f"cwd         : {os.getcwd()}",
            f"pid         : {os.getpid()}",
            "",
            "env:",
        ]
        for key, value in sorted(os.environ.items()):
            lines.append(f"  {key}={value}")
        await _text_response(send, "\n".join(lines))
        return

    webhook_prefix = "/webhook/"
    if method == "POST" and path.startswith(webhook_prefix):
        bot_token = unquote(path[len(webhook_prefix):])
        if bot_token:
            await _handle_webhook(bot_token, receive, send)
            return

    setwebhook_prefix = "/setwebhook/"
    if method == "GET" and path.startswith(setwebhook_prefix):
        bot_token = unquote(path[len(setwebhook_prefix):])
        if bot_token:
            webhook_url = f"{_base_url(scope)}/webhook/{bot_token}"
            result = await telegram_request(bot_token, "setWebhook", url=webhook_url)
            if result.get("ok"):
                await ensure_bot_commands(bot_token)
            logger.info("Đã đặt webhook cho bot: %s", webhook_url)
            await _json_response(send, result)
            return

    await _json_response(send, {"detail": "Not Found"}, 404)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
