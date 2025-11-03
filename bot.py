# bot.py
import os
import io
import random
import logging
import asyncio
import base64
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# Configuration / env
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEYS = [k.strip() for k in os.getenv("OPENAI_API_KEYS", os.getenv("OPENAI_API_KEY", "")).split(",") if k.strip()]
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", 10000))

FOIL_STAMP_PATH = os.getenv("FOIL_STAMP_PATH", "assets/Foil_stamp.png")
FOIL_SCALE = float(os.getenv("FOIL_SCALE", 0.13))
FOIL_X_OFFSET = float(os.getenv("FOIL_X_OFFSET", 0.0))
FOIL_Y_OFFSET = float(os.getenv("FOIL_Y_OFFSET", 0.0))

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", 3))
USER_COOLDOWN_SECONDS = int(os.getenv("USER_COOLDOWN_SECONDS", 300))

CARD_WIDTH = int(os.getenv("CARD_WIDTH", 1024))
CARD_HEIGHT = int(os.getenv("CARD_HEIGHT", 1536))
CARD_SIZE = f"{CARD_WIDTH}x{CARD_HEIGHT}"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required in environment variables")
if not OPENAI_API_KEYS:
    raise RuntimeError("OPENAI_API_KEYS (or OPENAI_API_KEY) must be set (comma separated)")

# Build webhook URL robustly
if RENDER_EXTERNAL_URL.lower().startswith("http://") or RENDER_EXTERNAL_URL.lower().startswith("https://"):
    WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}/webhook/{BOT_TOKEN}"
else:
    WEBHOOK_URL = f"https://{RENDER_EXTERNAL_URL}/webhook/{BOT_TOKEN}"

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("rizo-bot")

# -----------------------------
# Prompt Template (full)
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
# FastAPI + Telegram App
# -----------------------------
app = FastAPI()
telegram_app = Application.builder().token(BOT_TOKEN).build()

# -----------------------------
# OpenAI clients + concurrency
# -----------------------------
clients: List[AsyncOpenAI] = [AsyncOpenAI(api_key=k) for k in OPENAI_API_KEYS]
client_index = 0
clients_lock = asyncio.Lock()
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

def pick_client_round_robin() -> AsyncOpenAI:
    # simple round-robin; protected by lock when used async
    global client_index
    client = clients[client_index % len(clients)]
    client_index_local = client_index
    client_index = (client_index_local + 1) % len(clients)
    return client

async def pick_client() -> AsyncOpenAI:
    async with clients_lock:
        return pick_client_round_robin()

# -----------------------------
# Helper: generate/edit card
# -----------------------------
async def generate_rizo_card(image_bytes_io: io.BytesIO, prompt_text: str) -> Image.Image:
    """
    Use OpenAI images.edit if an image is provided (we pass the uploaded meme).
    Falls back to images.generate if edit fails.
    Returns a PIL Image.
    """
    image_bytes_io.seek(0)
    tried_keys = []
    last_exc: Optional[Exception] = None

    # We'll try a few times across clients
    for attempt in range(len(clients)):
        client = await pick_client()
        try:
            # For edit, pass a file-like object. AsyncOpenAI's images.edit accepts 'image' as file-like.
            image_bytes_io.seek(0)
            file_like = io.BytesIO(image_bytes_io.read())
            file_like.name = "meme.png"
            file_like.seek(0)

            full_prompt = f"Use this meme image as the main character for a RIZO card.\n\n{prompt_text}"

            log.info("Calling OpenAI images.edit (using uploaded meme as base)...")
            response = await client.images.edit(
                model="gpt-image-1",
                image=file_like,
                prompt=full_prompt,
                size=CARD_SIZE,
            )

            card_b64 = response.data[0].b64_json
            card_bytes = base64.b64decode(card_b64)
            return Image.open(io.BytesIO(card_bytes)).convert("RGBA")

        except Exception as e:
            log.warning("OpenAI images.edit failed on attempt %d with client: %s", attempt+1, e)
            last_exc = e
            # fallback to generate once after trying edit on each key? we'll try generate with same client
            try:
                client2 = client
                full_prompt = f"{prompt_text}\nUse the uploaded meme image as a reference (if available) and produce a RIZO trading card."
                log.info("Falling back to images.generate...")
                response = await client2.images.generate(
                    model="gpt-image-1",
                    prompt=full_prompt,
                    size=CARD_SIZE,
                )
                card_b64 = response.data[0].b64_json
                card_bytes = base64.b64decode(card_b64)
                return Image.open(io.BytesIO(card_bytes)).convert("RGBA")
            except Exception as e2:
                log.warning("OpenAI images.generate also failed: %s", e2)
                last_exc = e2
                continue

    # if all clients failed
    raise last_exc or RuntimeError("OpenAI image generation/edit failed with unknown error")

# -----------------------------
# Helper: foil stamp overlay
# -----------------------------
def add_foil_stamp(card_image: Image.Image, logo_path: str = FOIL_STAMP_PATH) -> io.BytesIO:
    card = card_image.convert("RGBA")
    logo = Image.open(logo_path).convert("RGBA")

    logo_width = int(card.width * FOIL_SCALE)
    if logo_width <= 0:
        logo_width = int(card.width * 0.13)
    ratio = logo_width / logo.width
    logo_height = int(logo.height * ratio)
    logo_resized = logo.resize((logo_width, logo_height), Image.LANCZOS)

    pos_x = int(card.width - logo_width + card.width * FOIL_X_OFFSET)
    pos_y = int(card.height - logo_height + card.height * FOIL_Y_OFFSET)

    # paste using alpha composite
    tmp = Image.new("RGBA", card.size)
    tmp.paste(logo_resized, (pos_x, pos_y), logo_resized)
    out = Image.alpha_composite(card, tmp)

    output = io.BytesIO()
    out.name = "rizo_card.png"
    out.seek(0)
    out.truncate(0)
    out_bytes = io.BytesIO()
    out.save(out_bytes, format="PNG")
    out_bytes.seek(0)
    return out_bytes

# -----------------------------
# Cooldowns state
# -----------------------------
user_last_request = {}  # user_id -> timestamp (float)

# -----------------------------
# Telegram handlers
# -----------------------------
async def cmd_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # set a flag telling user to send image
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    await update.message.reply_text("Send me a meme image and I'll make a RIZO card (I'll use the uploaded image as the base).")

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        await update.message.reply_text("Couldn't identify user.")
        return
    user_id = user.id

    # cooldown
    now = asyncio.get_event_loop().time()
    last = user_last_request.get(user_id, 0)
    if now - last < USER_COOLDOWN_SECONDS:
        remain = int(USER_COOLDOWN_SECONDS - (now - last))
        await update.message.reply_text(f"⏳ Please wait {remain}s before generating another card.")
        return
    user_last_request[user_id] = now

    if not update.message.photo:
        await update.message.reply_text("Please send a photo to generate a RIZO card.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
    await update.message.reply_text("🎨 Generating your RIZO card... this may take a few seconds.")

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    meme_bytes = io.BytesIO()
    await photo_file.download_to_memory(out=meme_bytes)
    meme_bytes.seek(0)

    async with semaphore:
        try:
            card_image = await generate_rizo_card(meme_bytes, PROMPT_TEMPLATE)
            final_bytes = add_foil_stamp(card_image, FOIL_STAMP_PATH)
            final_bytes.seek(0)

            keyboard = [[InlineKeyboardButton("🎨 Create another RIZO card", callback_data="create_another")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)
            await update.message.reply_photo(photo=final_bytes, caption="✨ Here’s your RIZO card!", reply_markup=reply_markup)
        except Exception as e:
            log.exception("Error generating card: %s", e)
            await update.message.reply_text(f"⚠️ Something went wrong: {e}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if query.data == "create_another":
        await query.message.reply_text("Awesome! Send me a new meme image and I'll make another RIZO card for you.")

# Register handlers
telegram_app.add_handler(CommandHandler("generate", cmd_generate))
telegram_app.add_handler(CommandHandler("start", cmd_generate))  # optional: point /start to same flow
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_image))
telegram_app.add_handler(CallbackQueryHandler(button_callback))

# -----------------------------
# FastAPI endpoints + lifecycle
# -----------------------------
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "✅ RIZO Card Bot (Docker) — live"

@app.post(f"/webhook/{BOT_TOKEN}")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    # application was initialized on startup (see on_startup)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    log.info("Starting telegram application...")
    # initialize and start the telegram app
    await telegram_app.initialize()
    await telegram_app.start()

    # set webhook with retry (robust)
    import httpx
    retries = 0
    while retries < 5:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook", params={"url": WEBHOOK_URL})
                log.info("Webhook set result: %s", resp.text)
                if resp.status_code == 200:
                    break
        except Exception as e:
            log.error("Webhook set attempt failed: %s", e)
        retries += 1
        await asyncio.sleep(2)
    log.info("Startup complete.")

@app.on_event("shutdown")
async def on_shutdown():
    log.info("Shutting down telegram application...")
    await telegram_app.stop()
    await telegram_app.shutdown()

# -----------------------------
# Run locally
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)
