import os
import io
import random
import logging
import asyncio
import base64
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from openai import AsyncOpenAI
from PIL import Image

# -----------------------------
# CONFIGURATION
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 10000))
WEBHOOK_URL = f"https://{os.getenv('RENDER_EXTERNAL_URL')}/webhook/{BOT_TOKEN}"

OPENAI_API_KEYS = os.getenv("OPENAI_API_KEYS", os.getenv("OPENAI_API_KEY", "")).split(",")

MAX_CONCURRENCY = 3
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

FOIL_STAMP_PATH = "assets/Foil_stamp.png"
FOIL_SCALE = float(os.getenv("FOIL_SCALE", 0.13))
FOIL_X_OFFSET = float(os.getenv("FOIL_X_OFFSET", 0.0))
FOIL_Y_OFFSET = float(os.getenv("FOIL_Y_OFFSET", 0.0))

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
# FastAPI & Telegram App
# -----------------------------
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# -----------------------------
# Helper Functions
# -----------------------------
def add_foil_stamp(card_image: Image.Image):
    """Overlay foil stamp in bottom-right corner."""
    card = card_image.convert("RGBA")
    logo = Image.open(FOIL_STAMP_PATH).convert("RGBA")

    # Scale foil
    logo_width = int(card.width * FOIL_SCALE)
    ratio = logo_width / logo.width
    logo_height = int(logo.height * ratio)
    logo_resized = logo.resize((logo_width, logo_height), Image.LANCZOS)

    # Position
    pos_x = int(card.width - logo_width + card.width * FOIL_X_OFFSET)
    pos_y = int(card.height - logo_height + card.height * FOIL_Y_OFFSET)

    card.alpha_composite(logo_resized, dest=(pos_x, pos_y))
    output = io.BytesIO()
    card.save(output, format="PNG")
    output.seek(0)
    output.name = "rizo_card.png"
    return output

# -----------------------------
# Telegram Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action(ChatAction.TYPING)
    await update.message.reply_text(
        "🎴 Welcome to RIZO Card Bot!\nSend me a meme image, and I'll turn it into a RIZO card!"
    )

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_chat_action(ChatAction.TYPING)
        await update.message.reply_text("⚠️ Please send a valid image!")
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    await update.message.reply_chat_action(ChatAction.TYPING)
    await update.message.reply_text("🎨 Generating your RIZO card... please wait!")

    async with semaphore:
        try:
            # Save uploaded image
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)

            # Generate card
            client = get_random_client()
            response = await client.images.generate(
                model="gpt-image-1",
                prompt=PROMPT_TEMPLATE,
                image=buf,
                size="1024x1024"
            )

            card_b64 = response.data[0].b64_json
            card_bytes = io.BytesIO(base64.b64decode(card_b64))
            card_bytes.seek(0)
            card_img = Image.open(card_bytes).convert("RGBA")

            # Add foil stamp
            final_card_bytes = add_foil_stamp(card_img)

            keyboard = [[InlineKeyboardButton("🎨 Create another RIZO card", callback_data="create_another")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)
            await update.message.reply_photo(
                photo=final_card_bytes,
                caption="✨ Here’s your RIZO card!",
                reply_markup=reply_markup
            )

        except Exception as e:
            logger.exception("Error generating card: %s", e)
            await update.message.reply_chat_action(ChatAction.TYPING)
            await update.message.reply_text(f"⚠️ Something went wrong: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_chat_action(ChatAction.TYPING)
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
# Startup & Shutdown
# -----------------------------
@app.on_event("startup")
async def on_startup():
    logger.info("Starting Telegram bot...")
    await telegram_app.initialize()
    await telegram_app.start()

    # Register webhook
    import httpx
    success = False
    retries = 0
    while not success and retries < 5:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                    params={"url": WEBHOOK_URL}
                )
            logger.info("Webhook set result: %s", resp.text)
            success = True
        except Exception as e:
            retries += 1
            logger.error("Failed to set webhook (attempt %d): %s", retries, e)
            await asyncio.sleep(2)

@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Stopping Telegram bot...")
    await telegram_app.stop()
    await telegram_app.shutdown()

# -----------------------------
# Run locally
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)
