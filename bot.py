import os
import io
import random
import logging
import asyncio
import base64
from datetime import datetime, timedelta
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
)
from openai import AsyncOpenAI
from PIL import Image

# -----------------------------
# CONFIGURATION
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
WEBHOOK_URL = f"https://{os.getenv('RENDER_EXTERNAL_URL')}/webhook/{BOT_TOKEN}"

OPENAI_API_KEYS = os.getenv("OPENAI_API_KEYS", os.getenv("OPENAI_API_KEY", "")).split(",")

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", 3))
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

USER_COOLDOWN_SECONDS = int(os.getenv("USER_COOLDOWN_SECONDS", 300))
FOIL_SCALE = float(os.getenv("FOIL_SCALE", 0.13))
FOIL_X_OFFSET = float(os.getenv("FOIL_X_OFFSET", 0.0))
FOIL_Y_OFFSET = float(os.getenv("FOIL_Y_OFFSET", 0.0))
FOIL_PATH = os.getenv("FOIL_PATH", "assets/Foil_stamp.png")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rizo-bot")

# -----------------------------
# OpenAI Clients
# -----------------------------
clients = [AsyncOpenAI(api_key=k.strip()) for k in OPENAI_API_KEYS if k.strip()]
if not clients:
    raise ValueError("No valid OpenAI API keys found in environment variables.")
client_index = 0

def get_next_client() -> AsyncOpenAI:
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
# FastAPI App
# -----------------------------
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# -----------------------------
# User Tracking
# -----------------------------
generate_requests = {}  # user_id -> timestamp of /generate
user_cooldowns = {}     # user_id -> timestamp of last generated card

# -----------------------------
# Helper Functions
# -----------------------------
async def generate_rizo_card(image_bytes_io: io.BytesIO, prompt_text: str):
    async with semaphore:
        client = get_next_client()
        image_bytes_io.seek(0)
        b64_image = base64.b64encode(image_bytes_io.read()).decode("utf-8")
        full_prompt = f"Use this meme image as the main character for a RIZO card.\n\n{prompt_text}"

        response = await client.images.generate(
            model="gpt-image-1",
            prompt=full_prompt,
            size="1024x1536",
            image=[{"b64_json": b64_image, "mime_type": "image/png"}]
        )

        card_b64 = response.data[0].b64_json
        return Image.open(io.BytesIO(base64.b64decode(card_b64)))

def add_foil_stamp(card_image: Image.Image):
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
# Telegram Handlers
# -----------------------------
async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    generate_requests[user_id] = datetime.utcnow()
    await update.message.reply_text("Send me your meme image now, and I'll create your RIZO card!")

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg_time = update.message.date

    # Ignore old messages
    if msg_time < datetime.utcnow() - timedelta(minutes=5):
        return

    # Only users who sent /generate can trigger
    if user_id not in generate_requests:
        return

    # Check cooldown
    last_generated = user_cooldowns.get(user_id)
    if last_generated and (datetime.utcnow() - last_generated).total_seconds() < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (datetime.utcnow() - last_generated).total_seconds())
        return await update.message.reply_text(f"⏳ You can generate another card in {remaining} seconds.")

    if not update.message.photo:
        await update.message.reply_text("Please send a valid image!")
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    # Typing indicator
    await update.message.chat.send_action(action="typing")

    try:
        card_image = await generate_rizo_card(io.BytesIO(image_bytes), PROMPT_TEMPLATE)
        card_with_stamp = add_foil_stamp(card_image)

        await update.message.reply_photo(
            photo=card_with_stamp,
            caption="✨ Here's your RIZO card!"
        )

        user_cooldowns[user_id] = datetime.utcnow()
        del generate_requests[user_id]

    except Exception as e:
        logger.exception("Error generating card: %s", e)
        await update.message.reply_text(f"⚠️ Something went wrong: {e}")

# Optional: button callback if needed
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "create_another":
        await query.message.reply_text("Send me a new meme image, and I'll make another RIZO card for you!")

# -----------------------------
# Register Handlers
# -----------------------------
telegram_app.add_handler(CommandHandler("generate", generate_command))
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
    logger.info("Starting Telegram bot...")
    await telegram_app.initialize()
    await telegram_app.start()
    import httpx
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                params={"url": WEBHOOK_URL}
            )
            logger.info("Webhook set result: %s", resp.text)
        except Exception as e:
            logger.error("Failed to set webhook: %s", e)

# -----------------------------
# Run app locally
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)
