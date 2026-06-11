"""
Telegram 机器人 — 群聊/频道关键词监听 + 随机视频转发
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
功能 1：关键词转发链（支持群聊 + 频道）
  源群聊/频道消息 → 识别关键词 → 转发给指定机器人 → 机器人回复 → 转发到你的频道

功能 2：未知群聊自动发现
  机器人加入新群后，只要有人说话，日志就会打印群 ID 和名称

功能 3：随机视频
  /random 随机转发一条历史视频，/stats 查看统计
"""

import os
import sqlite3
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

load_dotenv()

# ==================== 环境变量 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("未设置 BOT_TOKEN")

# --- 关键词转发链 ---
# 监听的来源（群聊或频道），逗号分隔。支持 @用户名 和 -100xxx 数字 ID
SOURCE_CHANNELS = [
    c.strip()
    for c in os.getenv("SOURCE_CHANNELS", "").split(",")
    if c.strip()
]
# 触发关键词，逗号分隔，不区分大小写，命中任意一个即触发
KEYWORDS = [
    k.strip()
    for k in os.getenv("KEYWORDS", "").split(",")
    if k.strip()
]
# 目标机器人 @用户名
TARGET_BOT = os.getenv("TARGET_BOT", "").strip()
# 目标机器人回复后转发到哪个频道
DEST_CHANNEL = os.getenv("DEST_CHANNEL", "").strip()

# --- 随机视频 ---
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

DB_PATH = "videos.db"

# 用于防止重复打印发现日志
_discovered_chats: set[int] = set()


# ==================== 工具函数 ====================
def is_chat_match(chat, config_id: str) -> bool:
    """判断一个 chat 是否匹配配置中的 ID（支持 @username 和数字 ID）"""
    chat_id_str = str(chat.id)
    expected = config_id.lstrip("@")

    if chat_id_str == expected:
        return True
    if chat.username and f"@{chat.username}" == config_id:
        return True
    return False


def any_keyword_match(text: str) -> bool:
    """文本中是否包含任意一个关键词（不区分大小写）"""
    if not text:
        return False
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in KEYWORDS)


def get_chat_label(chat) -> str:
    """返回便于阅读的 chat 标识"""
    parts = [str(chat.id)]
    title = getattr(chat, "title", "") or ""
    username = getattr(chat, "username", "") or ""
    if title:
        parts.append(f"'{title}'")
    if username:
        parts.append(f"@{username}")
    return ", ".join(parts)


# ==================== 数据库（随机视频）====================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                file_id TEXT,
                file_unique_id TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_video_unique
            ON videos(chat_id, message_id)
        """)


def add_video(message_id: int, chat_id: str, file_id: str, file_unique_id: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO videos (message_id, chat_id, file_id, file_unique_id) "
            "VALUES (?, ?, ?, ?)",
            (message_id, chat_id, file_id, file_unique_id),
        )


def get_random_video() -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT message_id, chat_id FROM videos ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_video_count() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM videos").fetchone()
    return row["cnt"] if row else 0


# ==================== 命令处理器 ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 你好！我是多功能转发机器人。\n\n"
        "📡 关键词转发链：监听群聊/频道 → 识别关键词 → 转给指定机器人 → 把回复转回你的频道\n\n"
        "🎲 命令列表：\n"
        "/random - 随机转发一条历史视频\n"
        "/stats  - 查看已收录的视频数量"
    )


async def random_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = get_random_video()
    if not video:
        await update.message.reply_text("📭 还没有收录任何视频，请先在频道中发布视频！")
        return

    try:
        await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=video["chat_id"],
            message_id=video["message_id"],
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 转发失败：{e}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = get_video_count()
    await update.message.reply_text(f"📊 已收录 {count} 条视频")


# ==================== 核心：消息处理（群聊 + 频道）====================
async def on_source_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    统一处理群聊消息和频道消息：
      1. 自动发现未知群聊/频道，打印 ID 到日志
      2. 检查关键词 → 转发给目标机器人
    """
    # 频道消息在 channel_post 字段，群聊消息在 message 字段
    msg = update.channel_post or update.message
    if msg is None:
        return

    chat = msg.chat
    chat_id = chat.id

    # ---------- 自动发现未知来源 ----------
    source_list = SOURCE_CHANNELS  # may be empty list
    is_source = any(is_chat_match(chat, ch) for ch in source_list)

    if not is_source and chat_id not in _discovered_chats:
        _discovered_chats.add(chat_id)
        chat_type = chat.type or "?"
        label = get_chat_label(chat)
        print("=" * 50)
        print(f"[🔍 发现新{ '群聊' if chat_type in ('group','supergroup') else '频道' }]")
        print(f"    ID: {chat_id}")
        print(f"    类型: {chat_type}")
        print(f"    名称: {label}")
        print(f"    把上面这个 ID 加到 Zeabur 环境变量 SOURCE_CHANNELS 即可开始监听")
        print("=" * 50)

    # ---------- 关键词转发 ----------
    if not SOURCE_CHANNELS or not KEYWORDS or not TARGET_BOT or not DEST_CHANNEL:
        return  # 配置不完整，只做发现不转发

    if not is_source:
        return

    text = msg.text or msg.caption or ""
    if not any_keyword_match(text):
        return

    # 命中！转发给目标机器人
    matched = [kw for kw in KEYWORDS if kw.lower() in text.lower()]
    try:
        await context.bot.forward_message(
            chat_id=TARGET_BOT,
            from_chat_id=chat_id,
            message_id=msg.message_id,
        )
        print(
            f"[{datetime.now()}] 🎯 命中 {matched} "
            f"→ 已转发至 {TARGET_BOT}（来源: {get_chat_label(chat)}）"
        )
    except Exception as e:
        print(f"[{datetime.now()}] ❌ 转发至 {TARGET_BOT} 失败: {e}")


# ==================== 目标机器人回复 → 转发到你的频道 ====================
async def on_target_bot_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """目标机器人回复 → 转发到目标频道"""
    msg = update.message
    if msg is None:
        return

    target_username = TARGET_BOT.lstrip("@")
    if not msg.from_user or msg.from_user.username != target_username:
        return

    try:
        await context.bot.forward_message(
            chat_id=DEST_CHANNEL,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        print(f"[{datetime.now()}] 📤 已转发 {TARGET_BOT} 的回复 → {DEST_CHANNEL}")
    except Exception as e:
        print(f"[{datetime.now()}] ❌ 转发至 {DEST_CHANNEL} 失败: {e}")


# ==================== 视频收录（/random 用）====================
async def on_channel_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if msg is None or msg.video is None:
        return

    if not CHANNEL_ID:
        return

    if not is_chat_match(msg.chat, CHANNEL_ID):
        return

    add_video(
        message_id=msg.message_id,
        chat_id=str(msg.chat_id),
        file_id=msg.video.file_id,
        file_unique_id=msg.video.file_unique_id,
    )
    print(f"[{datetime.now()}] ✅ 收录视频 message_id={msg.message_id}")


# ==================== 主程序 ====================
def main():
    init_db()
    print(f"📦 数据库就绪，当前收录 {get_video_count()} 条视频")

    # 打印当前配置
    print("=" * 50)
    print("当前配置：")
    print(f"  BOT_TOKEN:     {BOT_TOKEN[:8]}...（已隐藏）")
    print(f"  源频道/群聊:   {SOURCE_CHANNELS or '(未配置，仅自动发现)'}")
    print(f"  关键词:        {KEYWORDS or '(未配置)'}")
    print(f"  目标机器人:    {TARGET_BOT or '(未配置)'}")
    print(f"  转发目的地:    {DEST_CHANNEL or '(未配置)'}")
    print(f"  视频收录频道:  {CHANNEL_ID or '(未配置)'}")
    print("=" * 50)

    app = Application.builder().token(BOT_TOKEN).build()

    # --- 命令 ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_video))
    app.add_handler(CommandHandler("stats", stats))

    # --- 群聊 + 频道消息（始终启用：自动发现 + 关键词转发）---
    app.add_handler(MessageHandler(
        filters.ChatType.CHANNEL,
        on_source_message,
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS,  # 同时匹配 group 和 supergroup
        on_source_message,
    ))
    print("✅ 群聊/频道监听已启用（自动发现 + 关键词转发）")

    # --- 目标机器人回复 ---
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE,
        on_target_bot_reply,
    ))
    print("✅ 目标机器人回复监听已启用")

    # --- 视频收录 ---
    if CHANNEL_ID:
        app.add_handler(MessageHandler(
            filters.ChatType.CHANNEL & filters.VIDEO,
            on_channel_video,
        ))
        print("✅ 视频收录已启用")

    print("🤖 机器人启动中...")
    app.run_polling(
        allowed_updates=["channel_post", "message"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
