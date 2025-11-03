import os
import io
import random
import base64
import asyncio
import logging
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ChatAction
from openai import AsyncOpenAI
from PIL import Image

# -----------------------------
# CONFIG
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEYS = os.getenv("OPENAI_API_KEYS", os.getenv("OPENAI_API_KEY", "")).split(",")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip(" /")
PORT = int(os.getenv("PORT", 10000))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", 3))
USER_COOLDOWN_SECONDS = int(os.getenv("USER_COOLDOWN_SECONDS", 300))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in environment variables")

WEBHOOK_URL = f"{RENDER_URL}/webhook/{BOT_TOKEN}"

# -----------------------------
# LOGGING
# -----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rizo-bot")

# -----------------------------
# GLOBALS
# -----------------------------
clients = [AsyncOpenAI(api_key=k.strip()) for k in OPENAI_API_KEYS if k.strip()]
if not clients:
    raise ValueError("No valid OpenAI API keys found.")

def get_random_client() -> AsyncOpenAI:
    return random.choice(clients)

semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
user_cooldowns = {}

# -----------------------------
# PROMPT
# -----------------------------
PROMPT_TEMPLATE = """
Create a RIZO digital trading card using the uploaded meme image as the main character.

Design guidelines:
- Always invent a unique, creative character name that matches the personality or vibe of the uploaded image.
- ALWAYS add the word "RIZO" at the end of the character name.
- Maintain a balanced layout with well-spaced elements.
- Include all standard card elements: name, HP, element, two attacks, flavor text, and themed background/frame.

Layout & spacing rules:
- Top bar: Place the character name on the left, and always render “HP” followed by the number (e.g. HP***) on the right side.
  The HP text must be completely visible, never cropped, never stylized, and always use a clean card font.
  Place the elemental icon beside the HP number, leaving at least 15% horizontal spacing so they do not touch or overlap.
- Main art: Use the uploaded meme image as the character art, dynamically styled without changing the underlying character in the meme.
- Attack boxes: Include two creative attacks with names, icons, and damage numbers.
- Flavor text: Include EXACTLY ONE short, unique line beneath the attacks (no repetition or duplication).
- Footer: Weakness/resistance icons should be on the left. Leave a clear empty area in the bottom-right corner for an official foil stamp.
- The foil stamp area must stay completely blank — do not draw or add any art or borders there.
- The foil stamp is a subtle circular authenticity mark that will be imprinted later.
- Overall aesthetic: vintage, realistic, collectible, with slight texture and warmth, but without altering any provided logos.
"""

# -----------------------------
# FASTAPI + TELEGRAM APP
# -----------------------------
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# -----------------------------
# HANDLERS
# -----------------------------
--- Commands ---
async def cms_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    await update.message.reply_text(
        "🎴 Welcome to the RIZO Card Bot!\n\n"
        "Send me a RIZO meme/image, and I’ll turn it into a unique HaHaYes RIZO trading card!"
    )

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = asyncio.get_event_loop().time()

    # Check cooldown
    if user_id in user_cooldowns and now - user_cooldowns[user_id] < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - user_cooldowns[user_id]))
        await update.message.reply_text(f"⏳ Please wait {remaining}s before generating another card.")
        return
    user_cooldowns[user_id] = now

    # Validate image
    if not update.message.photo:
        await update.message.reply_text("Please send an image!")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    await update.message.reply_text("🎨 Generating your RIZO card... back in 2 minutes!")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    async with semaphore:
        try:
            # Prepare image for OpenAI
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)

            client = get_random_client()
            response = await client.images.edit(
                model="gpt-image-1",
                prompt=PROMPT_TEMPLATE,
                size="1024x1536"
            )

            card_b64 = response.data[0].b64_json
            card_bytes = io.BytesIO(base64.b64decode(card_b64))
            card_bytes.name = "rizo_card.png"

            keyboard = [[InlineKeyboardButton("✨ Create another", callback_data="create_another")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
            await update.message.reply_photo(
                photo=card_bytes,
                caption="🎴 Here's your RIZO card!",
                reply_markup=reply_markup,
            )

        except Exception as e:
            logger.exception("Error generating card: %s", e)
            await update.message.reply_text(f"⚠️ Error generating card: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "create_another":
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
        await query.message.reply_text("Send me a new meme image and I’ll make another RIZO card!")

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("generate", cmd_generate))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_image))
telegram_app.add_handler(CallbackQueryHandler(button_callback))

# -----------------------------
# FASTAPI ROUTES
# -----------------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "✅ RIZO Bot is live!"

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.initialize()
    await telegram_app.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            params={"url": WEBHOOK_URL}
        )
        logger.info(f"Webhook set: {resp.text}")

# -----------------------------
# LOCAL RUN
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)
