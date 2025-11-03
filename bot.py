import os
import io
import random
import logging
import asyncio
import base64
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from openai import AsyncOpenAI
from PIL import Image

# -----------------------------
# CONFIGURATION
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
WEBHOOK_URL = f"https://{os.getenv('RENDER_EXTERNAL_URL')}/webhook/{BOT_TOKEN}"

# OpenAI keys (comma-separated)
OPENAI_API_KEYS = os.getenv("OPENAI_API_KEYS", os.getenv("OPENAI_API_KEY", "")).split(",")

MAX_CONCURRENCY = 3
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rizo-bot")

# -----------------------------
# OpenAI Clients
# -----------------------------
clients = [AsyncOpenAI(api_key=k.strip()) for k in OPENAI_API_KEYS if k.strip()]
if not clients:
    raise ValueError("No valid OpenAI API keys found in environment variables.")

def get_random_client() -> AsyncOpenAI:
    return random.choice(clients)

# -----------------------------
# Prompt Template
# -----------------------------
PROMPT_TEMPLATE = """
Create a RIZO digital trading card using the uploaded meme image as the main character.

Design guidelines:
- Always invent a unique, creative character name that matches the personality or vibe of the uploaded image.
- ALWAYS add the word "RIZO" at the end of the character name.
- Maintain a balanced layout with well-spaced elements.
- Include all standard card elements: name, HP, element, two attacks, flavor text, and themed background/frame.

Layout & spacing rules:
- Top bar: Place the character name on the left, and always render “HP” followed by the number (e.g. HP100) on the right side.
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
# FastAPI App
# -----------------------------
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# -----------------------------
# Telegram Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎴 Welcome to RIZO Card Bot!\nSend me a meme image, and I'll turn it into a RIZO card!"
    )

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please send a valid image!")
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    await update.message.reply_text("🎨 Generating your RIZO card... please wait!")

    async with semaphore:
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)

            client = get_random_client()
            response = await client.images.generate(
                model="gpt-image-1",
                prompt=PROMPT_TEMPLATE,
                image=buf,
                size="1024x1024"
            )

            card_b64 = response.data[0].b64_json
            card_bytes = io.BytesIO(base64.b64decode(card_b64))
            card_bytes.name = "rizo_card.png"

            keyboard = [[InlineKeyboardButton("🎨 Create another RIZO card", callback_data="create_another")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_photo(
                photo=card_bytes,
                caption="✨ Here’s your RIZO card!",
                reply_markup=reply_markup
            )

        except Exception as e:
            logger.exception("Error generating card: %s", e)
            await update.message.reply_text(f"⚠️ Something went wrong: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "create_another":
        await query.message.reply_text("Send me a new meme image, and I'll make another RIZO card for you!")

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_image))
telegram_app.add_handler(CallbackQueryHandler(button_callback))

# -----------------------------
# FastAPI Routes
# -----------------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "✅ Rizo Bot is live!"

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

# -----------------------------
# Startup Event: Auto Webhook
# -----------------------------
@app.on_event("startup")
async def on_startup():
    logger.info("Setting Telegram webhook...")
    import httpx
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook", params={"url": WEBHOOK_URL})
            logger.info("Webhook set result: %s", resp.text)
        except Exception as e:
            logger.error("Failed to set webhook: %s", e)

# -----------------------------
# Run app locally
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)
