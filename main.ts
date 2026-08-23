// Deno / Val Town serverless port của bot gắn nhãn Telegram.
// Hàm entry là một HTTP function duy nhất: export default async (req) => Response.
// Các route:
//   POST /webhook/:bot_token  -> nhận update từ Telegram
//   GET  /setwebhook/:bot_token -> đăng ký webhook tự động từ base url
//   GET  /                    -> health check

const TAGS = ["VừaĐuỵchVợBạn", "Quên Chùi Đuých", "Vừa Tè Bậy", "Thèm Cặc"];
const TAGS_1 = [
  "Thích",
  "Cư Sĩ",
  "Thích Nữ",
  "Đạo Hữu",
  "Tôn Giả",
  "Chiến Thần",
  "Chí Tôn",
  "Tiên Sinh",
  "Hành Giả",
  "Trùm",
];
const TAGS_2 = [
  "Quay Tay",
  "Tè Bậy",
  "Sóc Lọ",
  "Thiểu Năng",
  "Xem Loèn",
  "Thẩm Du",
  "Buscu",
  "Mặt Lồn",
  "Mê Cức",
  "Làm Đũy",
  "Thèm Cặc",
  "Óc Lồn",
  "Mộng Du",
];

// --- Giới hạn tốc độ (token bucket), tránh Telegram 429 ---
const RATE_LIMIT_PER_SEC = 25;
const BUCKET_CAPACITY = 25;
// Đổi nhãn tối đa 1 lần / 60s / người dùng
const USER_TAG_COOLDOWN_SECONDS = 60;

// State toàn cục (sẽ reset giữa các cold start trong serverless, chấp nhận được)
const _buckets = new Map<string, TokenBucket>();
const _userTagLocks = new Map<string, Promise<void>>();
const _userLastTag = new Map<string, number>();
const _botUserIdCache = new Map<string, number>();

function lockKey(botToken: string, chatId: number, userId: number): string {
  return `${botToken}:${chatId}:${userId}`;
}

// Token bucket đơn giản, async để chờ refill khi cạn
class TokenBucket {
  rate: number;
  capacity: number;
  tokens: number;
  updated: number;

  constructor(rate: number, capacity: number) {
    this.rate = rate;
    this.capacity = capacity;
    this.tokens = capacity;
    this.updated = performance.now() / 1000;
  }

  async acquire(): Promise<void> {
    const now = performance.now() / 1000;
    this.tokens = Math.min(
      this.capacity,
      this.tokens + (now - this.updated) * this.rate,
    );
    this.updated = now;
    if (this.tokens < 1) {
      await new Promise((r) =>
        setTimeout(r, ((1 - this.tokens) / this.rate) * 1000)
      );
      this.tokens = 0;
      this.updated = performance.now() / 1000;
    } else {
      this.tokens -= 1;
    }
  }
}

function getBucket(botToken: string): TokenBucket {
  let bucket = _buckets.get(botToken);
  if (!bucket) {
    bucket = new TokenBucket(RATE_LIMIT_PER_SEC, BUCKET_CAPACITY);
    _buckets.set(botToken, bucket);
  }
  return bucket;
}

async function telegramRequest(
  botToken: string,
  method: string,
  params: Record<string, unknown> = {},
): Promise<any> {
  await getBucket(botToken).acquire();
  const url = `https://api.telegram.org/bot${botToken}/${method}`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return await resp.json();
}

async function getBotUserId(botToken: string): Promise<number> {
  if (!_botUserIdCache.has(botToken)) {
    const me = await telegramRequest(botToken, "getMe");
    _botUserIdCache.set(botToken, me.result.id);
  }
  return _botUserIdCache.get(botToken)!;
}

async function isBotAdmin(botToken: string, chatId: number): Promise<boolean> {
  const botUserId = await getBotUserId(botToken);
  const result = await telegramRequest(botToken, "getChatMember", {
    chat_id: chatId,
    user_id: botUserId,
  });
  const status = result?.result?.status ?? "";
  return status === "administrator" || status === "creator";
}

async function isUserAdmin(
  botToken: string,
  chatId: number,
  userId: number,
): Promise<boolean> {
  const result = await telegramRequest(botToken, "getChatMember", {
    chat_id: chatId,
    user_id: userId,
  });
  const status = result?.result?.status ?? "";
  return status === "administrator" || status === "creator";
}

async function setUserTag(
  botToken: string,
  chatId: number,
  userId: number,
  tag: string,
): Promise<any> {
  return await telegramRequest(botToken, "setChatMemberTag", {
    chat_id: chatId,
    user_id: userId,
    tag: tag.slice(0, 16),
  });
}

async function handleWebhook(botToken: string, body: any): Promise<Response> {
  if (!body || !body.message) return json({ ok: true });

  const chat = body.message.chat ?? {};
  const chatId = chat.id;
  const chatType = chat.type;

  if (chatType !== "group" && chatType !== "supergroup") {
    return json({ ok: true });
  }

  const fromUser = body.message.from ?? {};
  const userId = fromUser.id;

  const botUserId = await getBotUserId(botToken);
  if (userId === botUserId) return json({ ok: true });

  if (!(await isBotAdmin(botToken, chatId))) {
    console.log(`Bot không phải admin ở nhóm ${chatId}, bỏ qua.`);
    return json({ ok: true });
  }

  if (await isUserAdmin(botToken, chatId, userId)) {
    console.log(`Người dùng ${userId} là admin, không đổi nhãn.`);
    return json({ ok: true });
  }

  const key = lockKey(botToken, chatId, userId);
  const now = performance.now() / 1000;
  const last = _userLastTag.get(key) ?? 0;

  // Cooldown giữa các lần đổi nhãn
  if (now - last < USER_TAG_COOLDOWN_SECONDS) {
    return json({ ok: true });
  }

  // Khóa theo chuỗi promise để tránh 2 update cùng lúc đổi trùng
  const prev = _userTagLocks.get(key) ?? Promise.resolve();
  const next = prev.then(async () => {
    const now2 = performance.now() / 1000;
    if (now2 - (_userLastTag.get(key) ?? 0) < USER_TAG_COOLDOWN_SECONDS) {
      return;
    }
    const t1 = TAGS_1[Math.floor(Math.random() * TAGS_1.length)];
    const t2 = TAGS_2[Math.floor(Math.random() * TAGS_2.length)];
    const tFinal = `${t1} ${t2}`;
    const taio = TAGS[Math.floor(Math.random() * TAGS.length)];
    const newTag = Math.random() < 0.5 ? tFinal : taio;
    try {
      const result = await setUserTag(botToken, chatId, userId, newTag);
      if (!result?.ok) {
        console.warn(`setChatMemberTag thất bại: ${result?.description}`);
      } else {
        _userLastTag.set(key, performance.now() / 1000);
        console.log(
          `Đã gắn nhãn cho user ${userId} thành '${newTag}' trong nhóm ${chatId}`,
        );
      }
    } catch (e) {
      console.error(`Lỗi khi gắn nhãn cho user ${userId}: ${e}`);
    }
  });
  // Giữ lại lock, dọn sau khi xong
  _userTagLocks.set(
    key,
    next.then(() => {}).catch(() => {}),
  );

  return json({ ok: true });
}

async function handleSetWebhook(req: Request, botToken: string): Promise<Response> {
  const baseUrl = new URL(req.url).origin;
  const webhookUrl = `${baseUrl}/webhook/${botToken}`;
  const result = await telegramRequest(botToken, "setWebhook", {
    url: webhookUrl,
  });
  console.log(`Đã đặt webhook cho bot: ${webhookUrl}`);
  return json(result);
}

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    headers: { "Content-Type": "application/json" },
  });
}

export default async function (req: Request): Promise<Response> {
  const url = new URL(req.url);
  const parts = url.pathname.split("/").filter(Boolean); // ["webhook", token] or ["setwebhook", token]

  if (parts.length === 2 && parts[0] === "webhook") {
    if (req.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }
    let body: any;
    try {
      body = await req.json();
    } catch {
      return new Response("Invalid JSON", { status: 400 });
    }
    return await handleWebhook(parts[1], body);
  }

  if (parts.length === 2 && parts[0] === "setwebhook") {
    if (req.method !== "GET") {
      return new Response("Method Not Allowed", { status: 405 });
    }
    return await handleSetWebhook(req, parts[1]);
  }

  // Health check
  return json({ status: "running", bot: "Telegram Webhook Bot" });
}
