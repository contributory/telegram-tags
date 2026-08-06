import asyncio
import logging
import random
import time

import httpx
from fastapi import FastAPI, HTTPException, Request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Bot token được truyền qua path param của webhook: POST /webhook/{bot_token}
WEBHOOK_PATH = "/webhook/{bot_token}"
TAGS = [
    "Vừa Mới Quay Tay",
    "Tè Quần Sáng Nay",
    "Ngồi Trúng Shit Chó",
    "Đang Bận Sóc Lọ",
    "Tao Bị Thiểu Năng",
    "Thích Ngủ Với Vợ Bạn",
    "Học Sinh Cấp 2",
    "Fan Của Thằng Lồn",
    "Đang Bận Ăn Cứt",
    "Thích Ngủ Với Chó",
    "Hay Bị Mộng Tinh",
    "Chiến Thần Thẩm Du",
    "Thèm Buscu",
    "Spam Bot Của Thằng Lồn",
    "Tây Môn Khánh",
]

# Cache user_id của bot theo token (hỗ trợ nhiều bot trên cùng một server)
_bot_user_id_cache: dict[str, int] = {}

# --- Cơ chế khóa / giới hạn tốc độ để tránh spam Telegram API (bị 429) ---
# Telegram giới hạn ~30 request/giây/bot, giữ dưới ngưỡng để an toàn.
RATE_LIMIT_PER_SEC = 25
BUCKET_CAPACITY = 25
# Chỉ đổi nhãn tối đa 1 lần / 60 giây / người dùng (tránh spam setChatMemberTag)
USER_TAG_COOLDOWN_SECONDS = 60

_buckets: dict[str, "TokenBucket"] = {}
_user_tag_locks: dict[tuple[str, int, int], asyncio.Lock] = {}
_user_last_tag: dict[tuple[str, int, int], float] = {}


class TokenBucket:
    """Token bucket giới hạn số request/giây gửi tới Telegram API."""

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
    # Đi qua token bucket trước khi gọi API để tránh bị Telegram giới hạn
    await _get_bucket(bot_token).acquire()
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=params)
        return resp.json()


async def get_bot_user_id(bot_token: str) -> int:
    """Lấy user_id của bot từ getMe (kèm cache theo token)."""
    if bot_token not in _bot_user_id_cache:
        me = await telegram_request(bot_token, "getMe")
        _bot_user_id_cache[bot_token] = me["result"]["id"]
    return _bot_user_id_cache[bot_token]


async def is_bot_admin(bot_token: str, chat_id: int) -> bool:
    """Kiểm tra xem bot có phải admin của nhóm không."""
    bot_user_id = await get_bot_user_id(bot_token)
    result = await telegram_request(
        bot_token, "getChatMember", chat_id=chat_id, user_id=bot_user_id
    )
    status = result.get("result", {}).get("status", "")
    return status in ("administrator", "creator")


async def is_user_admin(bot_token: str, chat_id: int, user_id: int) -> bool:
    """Kiểm tra xem người dùng có phải admin không."""
    result = await telegram_request(
        bot_token, "getChatMember", chat_id=chat_id, user_id=user_id
    )
    status = result.get("result", {}).get("status", "")
    return status in ("administrator", "creator")


async def set_user_tag(bot_token: str, chat_id: int, user_id: int, tag: str) -> dict:
    """
    Gắn nhãn (custom tag / title) cho người dùng trong nhóm bằng API mới
    setChatMemberTag - cho phép gắn nhãn cho TẤT CẢ thành viên, không chỉ admin.
    Tag tối đa 16 ký tự.
    """
    return await telegram_request(
        bot_token, "setChatMemberTag", chat_id=chat_id, user_id=user_id, tag=tag[:16]
    )


@app.post(WEBHOOK_PATH)
async def webhook(request: Request, bot_token: str):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if "message" not in body:
        return {"ok": True}

    message = body["message"]
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")

    # Chỉ xử lý tin nhắn trong nhóm (group hoặc supergroup)
    if chat_type not in ("group", "supergroup"):
        return {"ok": True}

    from_user = message.get("from", {})
    user_id = from_user.get("id")

    # Bỏ qua nếu là tin nhắn từ chính bot
    bot_user_id = await get_bot_user_id(bot_token)
    if user_id == bot_user_id:
        return {"ok": True}

    # Kiểm tra bot có phải admin không
    if not await is_bot_admin(bot_token, chat_id):
        logger.info("Bot không phải admin ở nhóm %s, bỏ qua.", chat_id)
        return {"ok": True}

    # Kiểm tra người gửi có phải admin không
    if await is_user_admin(bot_token, chat_id, user_id):
        logger.info("Người dùng %s là admin, không đổi nhãn.", user_id)
        return {"ok": True}

    # Cơ chế khóa: tránh spam đổi nhãn cùng một người dùng
    key = (bot_token, chat_id, user_id)

    # Cooldown: bỏ qua nếu người dùng vừa được đổi nhãn gần đây
    if time.monotonic() - _user_last_tag.get(key, 0.0) < USER_TAG_COOLDOWN_SECONDS:
        return {"ok": True}

    lock = _user_tag_locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Kiểm tra lại sau khi giành được lock (tránh đổi trùng khi 2 tin nhắn cùng lúc)
        if time.monotonic() - _user_last_tag.get(key, 0.0) < USER_TAG_COOLDOWN_SECONDS:
            return {"ok": True}

        # Gắn nhãn (tag) cho người dùng không phải admin
        new_tag = random.choice(TAGS)
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

    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "running", "bot": "Telegram Webhook Bot"}


@app.get("/setwebhook/{bot_token}")
async def set_webhook(request: Request, bot_token: str):
    """
    Đặt webhook cho bot bằng token truyền qua path param (GET).
    Không dùng env - URL webhook được tự động tạo từ request hiện tại:
    {base_url}/webhook/{bot_token}
    """
    base_url = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/webhook/{bot_token}"
    result = await telegram_request(bot_token, "setWebhook", url=webhook_url)
    logger.info("Đã đặt webhook cho bot: %s", webhook_url)
    return result
