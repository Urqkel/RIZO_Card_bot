import os
import io
import random
import base64
import asyncio
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from openai import AsyncOpenAI
from PIL import Image

# -----------------------------
# CONFIGURATION
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "")
WEBHOOK_URL = f"https://{RENDER_URL}/webhook/{BOT_TOKEN}"

OPENAI_API_KEYS = os.getenv("OPENAI_API_KEYS", "").split(",")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", 3))
USER_COOLDOWN_SECONDS = int(os.getenv("USER_COOLDOWN_SECONDS", 300))

# Foil stamp placement ENV vars
FOIL_SCALE = float(os.getenv("FOIL_SCALE", 0.13))
FOIL_X_OFFSET = float(os.getenv("FOIL_X_OFFSET", 0.0))
FOIL_Y_OFFSET = float(os.getenv("FOIL_Y_OFFSET", 0.0))
FOIL_PATH = os.getenv("FOIL_PATH", "assets/Foil_stamp.png")

# -----------------------------
# LOGGING
# -----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rizo-bot")

# -----------------------------
# OpenAI setup
# -----------------------------
clients = [AsyncOpenAI(api_key=k.strip()) for k in OPENAI_API_KEYS if k.strip()]
if not clients:
    raise ValueError("No valid OpenAI API keys found.")
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
client_index = 0

def get_client():
    global client_index
    client = clients[client_index]
    client_index = (client_index + 1) % len(clients)
    return client

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
# STATE MANAGEMENT
# -----------------------------
user_cooldowns = {}
awaiting_images = {}

# -----------------------------
# HELPER FUNCTIONS
# -----------------------------
async def generate_rizo_card(image_bytes_io: io.BytesIO, prompt_text: str):
    async with semaphore:
        client = get_client()
        image_bytes_io.seek(0)
        b64_image = base64.b64encode(image_bytes_io.read()).decode("utf-8")

        response = await client.images.generate(
            model="gpt-image-1",
            prompt=prompt_text,
            size="1024x1536",
            image=[{"b64_json": b64_image, "mime_type": "image/png"}],
        )

        card_b64 = response.data[0].b64_json
        return Image.open(io.BytesIO(base64.b64decode(card_b64)))

def add_foil_stamp(card_image: Image.Image):
    if not os.path.exists(FOIL_PATH):
        return card_image

    card = card_image.convert("RGBA")
    logo = Image.open(FOIL_PATH).convert("RGBA")

    logo_width = int(card.width * FOIL_SCALE)
    ratio = logo_width / logo.width
    logo_height = int(logo.height * ratio)
    logo_resized = logo.resize((logo_width, logo_height), Image.LANCZOS)

    pos_x = int(card.width - logo_width + card.width * FOIL_X_OFFSET)
    pos_y = int(card.height - logo_height + card.height * FOIL_Y_OFFSET)

    card.alpha_composite(logo_resized, dest=(pos_x, pos_y))
    output = io.BytesIO()
    card.save(output, format="PNG")
    output.seek(0)
    return output

# -----------------------------
# TELEGRAM HANDLERS
# -----------------------------
async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.utcnow()

    # Cooldown check
    if user_id in user_cooldowns and now < user_cooldowns[user_id]:
        remaining = int((user_cooldowns[user_id] - now).total_seconds())
        await update.message.reply_text(f"⏳ Please wait {remaining}s before generating another RIZO card.")
        return

    awaiting_images[user_id] = now + timedelta(minutes=2)
    await update.message.reply_text("🎴 Send me a meme image to generate your RIZO card!")

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.utcnow()

    if user_id not in awaiting_images or now > awaiting_images[user_id]:
        return  # Ignore random images

    del awaiting_images[user_id]
    user_cooldowns[user_id] = now + timedelta(seconds=USER_COOLDOWN_SECONDS)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    await update.message.reply_text("🎨 Generating your RIZO card... please wait!")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        card_img = await generate_rizo_card(buf, PROMPT_TEMPLATE)
        stamped = add_foil_stamp(card_img)

        keyboard = [[InlineKeyboardButton("🎨 Create another", callback_data="generate_another")]]
        await update.message.reply_photo(
            photo=stamped,
            caption="✨ Here’s your RIZO card!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logger.exception("Error generating card: %s", e)
        await update.message.reply_text(f"⚠️ Something went wrong: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "generate_another":
        await cmd_generate(update, context)

# -----------------------------
# APP + ROUTES
# -----------------------------
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

telegram_app.add_handler(CommandHandler("generate", cmd_generate))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_image))
telegram_app.add_handler(CallbackQueryHandler(button_callback))

@app.get("/", response_class=PlainTextResponse)
async def root():
    return "✅ RIZO Card Bot is live!"

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    if not telegram_app.running:
        await telegram_app.initialize()
        await telegram_app.start()
    await telegram_app.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    logger.info("Setting Telegram webhook...")
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            params={"url": WEBHOOK_URL},
        )
        logger.info("Webhook set result: %s", resp.text)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)
