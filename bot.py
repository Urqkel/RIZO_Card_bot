import os
import io
import random
import base64
import logging
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
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

OPENAI_API_KEYS = os.getenv("OPENAI_API_KEYS", os.getenv("OPENAI_API_KEY", "")).split(",")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))

FOIL_SCALE = float(os.getenv("FOIL_SCALE", 0.13))
FOIL_X_OFFSET = float(os.getenv("FOIL_X_OFFSET", 0.0))
FOIL_Y_OFFSET = float(os.getenv("FOIL_Y_OFFSET", 0.0))
FOIL_PATH = os.getenv("FOIL_PATH", "assets/Foil_stamp.png")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rizo-bot")

# -----------------------------
# OpenAI Setup
# -----------------------------
clients = [AsyncOpenAI(api_key=k.strip()) for k in OPENAI_API_KEYS if k.strip()]
if not clients:
    raise ValueError("❌ No valid OpenAI API keys found.")

client_index = 0
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

PROMPT_TEXT = """
Create a RIZO digital trading card using the uploaded meme image as the main character.

Design guidelines:
- Always invent a unique, creative character name that matches the personality or vibe of the uploaded image.
- ALWAYS add the word "RIZO" at the end of the character name.
- Maintain a balanced layout with well-spaced elements.
- Include all standard card elements: name, HP, element, two attacks, flavor text, and themed background/frame.

Layout & spacing rules:
- Top bar: character name on left, “HP###” + element icon on right.
- The HP text must be fully visible and clean.
- Main art: use the uploaded meme as the character.
- Two creative attacks with names, icons, and damage numbers.
- One short unique flavor text line.
- Footer: Weakness/resistance icons on left.
- Leave the bottom-right corner blank for an official foil stamp.
- The foil stamp is a subtle circular authenticity mark to be added later.
- Aesthetic: vintage, collectible, realistic.
"""

# -----------------------------
# Helper Functions
# -----------------------------
async def generate_rizo_card(image_bytes_io: io.BytesIO, prompt_text: str):
    """Generate a RIZO card image using OpenAI image model."""
    global client_index
    async with semaphore:
        client = clients[client_index]
        client_index = (client_index + 1) % len(clients)

        image_bytes_io.seek(0)
        b64_image = base64.b64encode(image_bytes_io.read()).decode("utf-8")

        full_prompt = f"Use this meme image as the main character for a RIZO card.\n\n{prompt_text}"

        response = await client.images.generate(
            model="gpt-image-1",
            prompt=full_prompt,
            image=[{"b64_json": b64_image, "mime_type": "image/png"}],
            size="1024x1536"
        )

        card_b64 = response.data[0].b64_json
        return Image.open(io.BytesIO(base64.b64decode(card_b64)))


def add_foil_stamp(card_image: Image.Image):
    """Overlay foil authenticity stamp in bottom-right corner."""
    if not os.path.exists(FOIL_PATH):
        logger.warning(f"Foil stamp not found at {FOIL_PATH}. Skipping.")
        output = io.BytesIO()
        card_image.save(output, format="PNG")
        output.seek(0)
        return output

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
# FastAPI App + Telegram
# -----------------------------
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()
user_states = {}

# -----------------------------
# Handlers
# -----------------------------
async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User starts generation process."""
    user_states[update.effective_user.id] = True
    await update.message.reply_text("📸 Send me a meme image to create your RIZO card!")

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle meme image upload."""
    user_id = update.effective_user.id
    if not user_states.get(user_id):
        return  # Ignore unsolicited images

    user_states[user_id] = False  # Reset state
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    await update.message.reply_text("🎨 Generating your RIZO card... please wait!")

    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        image_bytes = await file.download_as_bytearray()

        img_io = io.BytesIO(image_bytes)
        generated_card = await generate_rizo_card(img_io, PROMPT_TEXT)
        stamped_output = add_foil_stamp(generated_card)

        keyboard = [[InlineKeyboardButton("🎴 Generate another RIZO card", callback_data="create_another")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_photo(
            photo=stamped_output,
            caption="✨ Here’s your official RIZO card!",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.exception("Error generating card: %s", e)
        await update.message.reply_text(f"⚠️ Something went wrong: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button."""
    query = update.callback_query
    await query.answer()
    if query.data == "create_another":
        user_states[query.from_user.id] = True
        await query.message.reply_text("📸 Send another meme image to create a new RIZO card!")

# -----------------------------
# Register Handlers
# -----------------------------
telegram_app.add_handler(CommandHandler("generate", cmd_generate))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_image))
telegram_app.add_handler(CallbackQueryHandler(button_callback))

# -----------------------------
# FastAPI Routes
# -----------------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "✅ Rizo Bot is live and ready!"

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    import httpx
    async with httpx.AsyncClient() as client:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
        resp = await client.get(url, params={"url": WEBHOOK_URL})
        logger.info("Webhook set result: %s", resp.text)

# -----------------------------
# Run Locally
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)
