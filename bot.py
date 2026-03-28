"""
bot.py  —  Telegram Filter Bot
───────────────────────────────
Features:
  ✅ Forced channel subscription (blocks access until joined)
  ✅ 3 private storage channels (searches all 3)
  ✅ Firebase Firestore database
  ✅ Forward videos directly from storage channels
"""

import os
import json
import logging
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─── CONFIG (environment variables) ───────────────────────────────────────────
BOT_TOKEN       = os.environ["BOT_TOKEN"]
ADMIN_IDS       = [int(x) for x in os.environ["ADMIN_IDS"].split(",")]
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "files")

# Public channel users MUST subscribe to (with or without @)
# e.g. "mychannel"  →  set MUST_JOIN = mychannel  (no @ needed)
MUST_JOIN       = os.environ["MUST_JOIN"]          # e.g. mychannel

# 3 private storage channels — bot must be admin in all 3
STORAGE_CHANNELS = [
    int(os.environ["STORAGE_CHANNEL_1"]),
    int(os.environ["STORAGE_CHANNEL_2"]),
    int(os.environ["STORAGE_CHANNEL_3"]),
]

# Firebase credentials as JSON string
_firebase_cred_dict = json.loads(os.environ["FIREBASE_CRED_JSON"])
# ───────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Firebase ────────────────────────────────────────────────────────────────────
cred = credentials.Certificate(_firebase_cred_dict)
firebase_admin.initialize_app(cred)
db   = firestore.client()

# ── DB helpers ──────────────────────────────────────────────────────────────────
def get_all_files() -> dict:
    docs = db.collection(COLLECTION_NAME).stream()
    return {doc.id: doc.to_dict() for doc in docs}

def save_file(name: str, data: dict):
    db.collection(COLLECTION_NAME).document(name).set(data)

def delete_file_db(name: str):
    db.collection(COLLECTION_NAME).document(name).delete()

def search_files(query: str) -> dict:
    all_files = get_all_files()
    return {k: v for k, v in all_files.items() if query.lower() in k.lower()}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ══════════════════════════════════════════════════════════════════════════════
#  SUBSCRIPTION CHECK
# ══════════════════════════════════════════════════════════════════════════════

async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if the user is a member of MUST_JOIN channel."""
    try:
        member = await context.bot.get_chat_member(
            chat_id=f"@{MUST_JOIN}",
            user_id=user_id,
        )
        return member.status in [
            ChatMember.MEMBER,
            ChatMember.OWNER,
            ChatMember.ADMINISTRATOR,
        ]
    except Exception as e:
        logger.warning(f"Subscription check failed for {user_id}: {e}")
        return False


async def send_subscribe_prompt(update: Update):
    """Send a join button to the user and block their request."""
    buttons = [[
        InlineKeyboardButton(
            "📢 Join Channel",
            url=f"https://t.me/{MUST_JOIN}"
        ),
        InlineKeyboardButton(
            "✅ I Joined",
            callback_data="check_sub"
        ),
    ]]
    await update.message.reply_text(
        "⚠️ *Access Restricted!*\n\n"
        "You must join our channel to use this bot.\n\n"
        "1️⃣ Click *Join Channel*\n"
        "2️⃣ Then click *I Joined* to continue",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the 'I Joined' button press."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    if await is_subscribed(user_id, context):
        await query.message.edit_text(
            "✅ *Access granted!*\n\nWelcome! Now type any movie name to search.",
            parse_mode="Markdown",
        )
    else:
        buttons = [[
            InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{MUST_JOIN}"),
            InlineKeyboardButton("✅ I Joined", callback_data="check_sub"),
        ]]
        await query.message.edit_text(
            "❌ *You haven't joined yet!*\n\n"
            "Please join the channel first, then click *I Joined*.",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )


# ── Decorator-style guard — call this at the top of every user handler ─────────
async def check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user can proceed. Sends prompt and returns False if not."""
    user_id = update.effective_user.id
    if is_admin(user_id):
        return True  # Admins always bypass
    if not await is_subscribed(user_id, context):
        await send_subscribe_prompt(update)
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  CORE: Send file — searches all 3 storage channels
# ══════════════════════════════════════════════════════════════════════════════

async def _send_file_by_name(chat_id: int, name: str, context: ContextTypes.DEFAULT_TYPE):
    doc = db.collection(COLLECTION_NAME).document(name).get()
    if not doc.exists:
        await context.bot.send_message(chat_id, f"❌ *{name}* not found.", parse_mode="Markdown")
        return

    entry = doc.to_dict()

    # Method 1: forward from whichever storage channel the file came from
    if "msg_id" in entry and "channel_id" in entry:
        try:
            await context.bot.forward_message(
                chat_id=chat_id,
                from_chat_id=entry["channel_id"],
                message_id=entry["msg_id"],
            )
            return
        except Exception as e:
            logger.warning(f"Forward failed for '{name}': {e} — trying file_id")

    # Method 2: file_id fallback (manually uploaded via /upload)
    if "file_id" in entry:
        try:
            await context.bot.send_video(
                chat_id=chat_id,
                video=entry["file_id"],
                caption=f"🎬 *{name}*",
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            logger.error(f"file_id send failed for '{name}': {e}")

    await context.bot.send_message(
        chat_id,
        "❌ Could not deliver the file. Please contact admin.",
        parse_mode="Markdown",
    )


async def _send_results(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    matches = search_files(query)

    if not matches:
        await update.message.reply_text(
            f"❌ No results for *{query}*.\n\nTry a different keyword.",
            parse_mode="Markdown",
        )
        return

    sorted_names = sorted(matches.keys())

    if len(sorted_names) == 1:
        name = sorted_names[0]
        await update.message.reply_text(f"🔍 Found: *{name}*\nSending...", parse_mode="Markdown")
        await _send_file_by_name(update.message.chat_id, name, context)
        return

    user_id = update.effective_user.id
    context.bot_data[f"results_{user_id}"] = sorted_names

    buttons = [
        [InlineKeyboardButton(name[:60], callback_data=f"si:{i}:{user_id}")]
        for i, name in enumerate(sorted_names)
    ]
    await update.message.reply_text(
        f"🔍 Found *{len(sorted_names)}* results for *{query}*. Choose one:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  USER COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return
    await update.message.reply_text(
        "👋 *Welcome to Filter Bot!*\n\n"
        "🎬 Type any movie or video name and I'll send it instantly.\n\n"
        "📌 *Commands:*\n"
        "  /search `<name>` — keyword search\n"
        "  /list — browse all available files",
        parse_mode="Markdown",
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return
    admin_text = (
        "\n\n🔧 *Admin Commands:*\n"
        "  Reply to a video + `/upload Movie Name`\n"
        "  `/delete Movie Name`\n"
        "  `/listall`"
        if is_admin(update.effective_user.id) else ""
    )
    await update.message.reply_text(
        "ℹ️ Just type any movie name and I'll find and send it!" + admin_text,
        parse_mode="Markdown",
    )

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: `/search Movie Name`", parse_mode="Markdown")
        return
    await _send_results(update, context, query)

async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return
    all_files = get_all_files()
    if not all_files:
        await update.message.reply_text("📭 No files stored yet.")
        return

    user_id = update.effective_user.id
    sorted_names = sorted(all_files.keys())
    context.bot_data[f"results_{user_id}"] = sorted_names

    buttons = [
        [InlineKeyboardButton(name[:60], callback_data=f"si:{i}:{user_id}")]
        for i, name in enumerate(sorted_names)
    ]
    await update.message.reply_text(
        f"📂 *{len(all_files)} files available — tap to receive:*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts   = query.data.split(":")
    index   = int(parts[1])
    user_id = int(parts[2])

    # Subscription check for button presses too
    if not is_admin(user_id):
        if not await is_subscribed(user_id, context):
            buttons = [[
                InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{MUST_JOIN}"),
                InlineKeyboardButton("✅ I Joined", callback_data="check_sub"),
            ]]
            await query.message.reply_text(
                "⚠️ Please join our channel first!",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

    names = context.bot_data.get(f"results_{user_id}", [])
    if not names or index >= len(names):
        await query.message.reply_text("⚠️ Session expired. Please search again.")
        return

    name = names[index]
    await _send_file_by_name(query.message.chat_id, name, context)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_subscription(update, context):
        return
    text = update.message.text.strip()
    await _send_results(update, context, text)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return
    replied = update.message.reply_to_message
    if not replied or not replied.video:
        await update.message.reply_text(
            "⚠️ Reply to a video with `/upload Movie Name`", parse_mode="Markdown"
        )
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("⚠️ `/upload Movie Name`", parse_mode="Markdown")
        return
    save_file(name, {
        "file_id":        replied.video.file_id,
        "file_unique_id": replied.video.file_unique_id,
        "file_name":      replied.video.file_name or name,
    })
    await update.message.reply_text(f"✅ *{name}* saved!", parse_mode="Markdown")

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.message.reply_text("Usage: `/delete Movie Name`", parse_mode="Markdown")
        return
    doc = db.collection(COLLECTION_NAME).document(name).get()
    if not doc.exists:
        await update.message.reply_text(f"❌ *{name}* not found.", parse_mode="Markdown")
        return
    delete_file_db(name)
    await update.message.reply_text(f"🗑️ *{name}* deleted.", parse_mode="Markdown")

async def list_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorised.")
        return
    all_files = get_all_files()
    if not all_files:
        await update.message.reply_text("📭 Database is empty.")
        return
    lines = "\n".join(f"• {name}" for name in sorted(all_files.keys()))
    await update.message.reply_text(
        f"📋 *Stored files ({len(all_files)}):*\n{lines}", parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("list", list_files))

    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(button_callback, pattern=r"^si:"))

    app.add_handler(CommandHandler("upload", upload))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("listall", list_all))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("✅ Bot running with subscription gate + 3 storage channels...")
    app.run_polling()

if __name__ == "__main__":
    main()
