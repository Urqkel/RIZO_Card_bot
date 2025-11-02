import os
import io
import base64
import asyncio
import random
import logging
from datetime import datetime, timedelta

import pytesseract
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from PIL import Image
from openai import OpenAI, OpenAIError

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rizo-bot")

# -----------------------------
# Configuration
# -----------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEYS = [k.strip() for k in os.getenv("OPENAI_KEYS", "").split(",") if k.strip()]
if not OPENAI_KEYS:
    raise RuntimeError("Please set OPENAI_KEYS (comma-separated) in your environment.")

FOIL_STAMP_PATH = os.getenv("FOIL_STAMP_PATH", "assets/Foil_stamp.png")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram_webhook")
PORT = int(os.environ.get("PORT", 10000))
DOMAIN = os.getenv("RENDER_EXTERNAL_URL", "https://your-render-domain.com")
WEBHOOK_URL = f"{DOMAIN}{WEBHOOK_PATH}"

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", 3))
USER_COOLDOWN_SECONDS = int(os.getenv("USER_COOLDOWN_SECONDS", 300))
FOIL_SCALE = float(os.getenv("FOIL_SCALE", 0.13))
FOIL_X_OFFSET = float(os.getenv("FOIL_X_OFFSET", 0.0))
FOIL_Y_OFFSET = float(os.getenv("FOIL_Y_OFFSET", 0.0))

# Semaphore to control concurrency
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# Track per-user cooldowns
user_last_request = {}  # user_id -> datetime

# Prompt template
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
# OpenAI Helper Functions
# -----------------------------
def pick_key(exclude_keys=None):
    """Pick a random OpenAI key, optionally excluding some."""
    candidates = [k for k in OPENAI_KEYS if not (exclude_keys and k in exclude_keys)]
    if not candidates:
        return random.choice(OPENAI_KEYS)
    return random.choice(candidates)

def create_openai_client(key: str):
    return OpenAI(api_key=key)

async def openai_images_edit_with_retries(image_io: io.BytesIO, prompt: str, size="1024x1536", retries=2):
    tried_keys = set()
    last_exc = None
    for attempt in range(retries + 1):
        key = pick_key(exclude_keys=tried_keys)
        tried_keys.add(key)
        client = create_openai_client(key)
        image_io.seek(0)
        try:
            def call_api():
                return client.images.edit(model="gpt-image-1", image=image_io, prompt=prompt, size=size)
            response = await asyncio.to_thread(call_api)
            card_b64 = response.data[0].b64_json
            return Image.open(io.BytesIO(base64.b64decode(card_b64)))
        except Exception as e:
            last_exc = e
            log.warning(f"OpenAI API call failed with key[{key[:8]}...]: {e}")
            await asyncio.sleep(1 + attempt * 2)
    raise last_exc

# -----------------------------
# Image Helpers
# -----------------------------
def add_foil_stamp(card_image: Image.Image, logo_path=FOIL_STAMP_PATH):
    card = card_image.convert("RGBA")
    logo = Image.open(logo_path).convert("RGBA")
    logo_width = int(card.width * FOIL_SCALE)
    ratio = logo_width / logo.width
    logo_height = int(logo.height * ratio)
    logo_resized = logo.resize((logo_width, logo_height), Image.LANCZOS)
    pos_x = int(card.width - logo_width + card.width * FOIL_X_OFFSET)
    pos_y = int(card.height - logo_height + card.height * FOIL_Y_OFFSET)
    tmp = Image.new("RGBA", card.size)
    tmp.paste(logo_resized, (pos_x, pos_y), logo_resized)
    card = Image.alpha_composite(card, tmp)
    output = io.BytesIO()
    card.save(output, format="PNG")
    output.seek(0)
    return output

def check_hp_visibility(card_image: Image.Image):
    try:
        text = pytesseract.image_to_string(card_image)
        return "HP" in text.upper()
    except Exception:
        return False

def check_flavor_text(card_image: Image.Image):
    try:
        text = pytesseract.image_to_string(card_image)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        candidates = [ln for ln in lines if 5 < len(ln) < 80 and "weak" not in ln.lower() and "resist" not in ln.lower()]
        return len(candidates) == len(list(dict.fromkeys(candidates)))
    except Exception:
        return True

# -----------------------------
# Telegram Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to RIZO card creator! Use /generate to create a RIZO meme card 🃏")

async def generate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["can_generate"] = True
    await update.message.reply_text("Send me a RIZO meme image, and I’ll craft a unique RIZO card for you 🃏")

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.utcnow()
    last = user_last_request.get(user_id)
    if last and (now - last).total_seconds() < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - last).total_seconds())
        await update.message.reply_text(f"⚠️ Please wait {remaining}s before generating another card.")
        return

    if not context.user_data.get("can_generate", False):
        await update.message.reply_text("⚠️ Please use /generate or click 'Create another RIZO card' before sending an image.")
        return

    context.user_data["can_generate"] = False
    user_last_request[user_id] = now

    await update.message.reply_text("🎨 Generating your RIZO card... hang tight!")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    meme_bytes_io = io.BytesIO()
    await photo_file.download_to_memory(out=meme_bytes_io)
    meme_bytes_io.seek(0)

    try:
        async with semaphore:
            card_image = await openai_images_edit_with_retries(meme_bytes_io, PROMPT_TEMPLATE, size="1024x1536")
            hp_ok, flavor_ok = await asyncio.gather(
                asyncio.to_thread(check_hp_visibility, card_image),
                asyncio.to_thread(check_flavor_text, card_image)
            )
            if not hp_ok:
                log.warning("HP text not detected on generated card.")
            if not flavor_ok:
                log.warning("Duplicate flavor text detected on generated card.")

            final_card_bytes = await asyncio.to_thread(add_foil_stamp, card_image)

            keyboard = [[InlineKeyboardButton("🎨 Create another RIZO card", callback_data="create_another")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            final_card_bytes.seek(0)
            await update.message.reply_photo(
                photo=final_card_bytes,
                caption="Here’s your RIZO card! 🃏",
                reply_markup=reply_markup
            )

    except Exception as exc:
        log.exception("Failed to generate card")
        await update.message.reply_text(f"Sorry, something went wrong: {exc}")
    finally:
        context.user_data["can_generate"] = True
        user_last_request[user_id] = datetime.utcnow()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "create_another":
        context.user_data["can_generate"] = True
        await query.message.reply_text("Awesome! Send me a new meme image, and I'll make another RIZO card for you.")

# -----------------------------
