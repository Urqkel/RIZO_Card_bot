# bot.py - RIZO Card Bot (FastAPI + python-telegram-bot)
import os
import io
import base64
import asyncio
import random
import logging
from datetime import datetime, timedelta

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
import pytesseract

# OpenAI v1.x SDK
from openai import OpenAI

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rizo-bot")

# -----------------------------
# Configuration (ENV)
# -----------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEYS = [k.strip() for k in os.getenv("OPENAI_KEYS", "").split(",") if k.strip()]
FOIL_STAMP_PATH = os.getenv("FOIL_STAMP_PATH", "assets/Foil_stamp.png")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram_webhook")
PORT = int(os.getenv("PORT", "10000"))
DOMAIN = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("DOMAIN") or "https://your-domain.example"
WEBHOOK_URL = f"{DOMAIN.rstrip('/')}{WEBHOOK_PATH}"

# concurrency/cooldown
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
USER_COOLDOWN_SECONDS = int(os.getenv("USER_COOLDOWN_SECONDS", "300"))

# foil offsets / scale
FOIL_SCALE = float(os.getenv("FOIL_SCALE", "0.13"))
FOIL_X_OFFSET = float(os.getenv("FOIL_X_OFFSET", "0.0"))
FOIL_Y_OFFSET = float(os.getenv("FOIL_Y_OFFSET", "0.0"))

# Retry attempts for OpenAI per-request
OPENAI_RETRY_ATTEMPTS = int(os.getenv("OPENAI_RETRY_ATTEMPTS", "2"))

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required")
if not OPENAI_KEYS:
    raise RuntimeError("OPENAI_KEYS env var is required (comma-separated OpenAI API keys)")

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
# Concurrency + cooldown tracking
# -----------------------------
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
user_last_request = {}  # user_id -> datetime

# -----------------------------
# OpenAI helpers (per-request client rotation + retry)
# -----------------------------
def pick_openai_key(exclude_keys=None):
    candidates = [k for k in OPENAI_KEYS if not (exclude_keys and k in exclude_keys)]
    if not candidates:
        # fallback to all if all excluded
        return random.choice(OPENAI_KEYS)
    return random.choice(candidates)

def create_openai_client(key: str):
    return OpenAI(api_key=key)

async def openai_images_edit_with_retries(image_io: io.BytesIO, prompt_text: str, size="1024x1536"):
    """Try multiple keys with simple retry/backoff. Returns PIL.Image on success."""
    tried = set()
    last_exc = None

    for attempt in range(OPENAI_RETRY_ATTEMPTS + 1):
        key = pick_openai_key(exclude_keys=tried)
        tried.add(key)
        client = create_openai_client(key)

        # ensure image stream at beginning for this attempt
        image_io.seek(0)

        try:
            # The openai client call might be blocking; run it in a thread
            def call_api():
                # This mirrors the Images Edit call pattern; adjust if your client version differs
                return client.images.edit(model="gpt-image-1", image=image_io, prompt=prompt_text, size=size)

            response = await asyncio.to_thread(call_api)
            # response.data[0].b64_json is expected from the images.edit response
            card_b64 = response.data[0].b64_json
            img = Image.open(io.BytesIO(base64.b64decode(card_b64)))
            return img

        except Exception as exc:
            last_exc = exc
            log.warning("OpenAI image call failed (key %s...): %s", key[:8], exc)
            # small backoff
            await asyncio.sleep(1 + attempt * 2)

    # if all tries exhausted, raise last exception
    raise last_exc

# -----------------------------
# Image processing helpers (blocking; run via to_thread)
# -----------------------------
def add_foil_stamp_sync(card_image: Image.Image, logo_path=FOIL_STAMP_PATH):
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

    out = io.BytesIO()
    card.save(out, format="PNG")
    out.seek(0)
    return out

def check_hp_visibility_sync(card_image: Image.Image):
    try:
        txt = pytesseract.image_to_string(card_image)
        return "HP" in txt.upper()
    except Exception as exc:
        log.warning("OCR HP check failed: %s", exc)
        return False

def check_flavor_text_sync(card_image: Image.Image):
    try:
        txt = pytesseract.image_to_string(card_image)
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        flavor_candidates = [ln for ln in lines if 5 < len(ln) < 80 and "weak" not in ln.lower() and "resist" not in ln.lower()]
        unique = list(dict.fromkeys(flavor_candidates))
        return len(flavor_candidates) == len(unique)
    except Exception as exc:
        log.warning("OCR flavor check failed: %s", exc)
        return True

# -----------------------------
# Telegram handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to RIZO card creator! Use /generate to create a RIZO meme card 🃏")

async def generate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["can_generate"] = True
    await update.message.reply_text("Send me a RIZO meme image, and I’ll craft a unique RIZO card for you 🃏")

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        await update.message.reply_text("Couldn't identify user.")
        return

    user_id = user.id
    now = datetime.utcnow()

    # cooldown check
    last = user_last_request.get(user_id)
    if last and (now - last).total_seconds() < USER_COOLDOWN_SECONDS:
        remain = int(USER_COOLDOWN_SECONDS - (now - last).total_seconds())
        await update.message.reply_text(f"⚠️ Please wait {remain}s before generating another card.")
        return

    if not context.user_data.get("can_generate", False):
        await update.message.reply_text("⚠️ Please use /generate or press 'Create another RIZO card' before sending an image.")
        return

    # mark user as busy/cooldown
    context.user_data["can_generate"] = False
    user_last_request[user_id] = now

    await update.message.reply_text("🎨 Generating your RIZO card... hang tight!")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # download photo
    try:
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()
        meme_bytes = io.BytesIO()
        await photo_file.download_to_memory(out=meme_bytes)
        meme_bytes.seek(0)
    except Exception as exc:
        log.exception("Failed to download photo: %s", exc)
        context.user_data["can_generate"] = True
        await update.message.reply_text("Failed to download your image. Try again.")
        return

    try:
        async with semaphore:
            # 1) OpenAI image edit with retries (runs blocking network in thread inside helper)
            card_image = await openai_images_edit_with_retries(meme_bytes, PROMPT_TEMPLATE, size="1024x1536")

            # 2) OCR checks (run in threads)
            hp_task = asyncio.to_thread(check_hp_visibility_sync, card_image)
            flav_task = asyncio.to_thread(check_flavor_text_sync, card_image)
            hp_ok, flavor_ok = await asyncio.gather(hp_task, flav_task)

            if not hp_ok:
                log.warning("HP not detected in generated card for user %s", user_id)
            if not flavor_ok:
                log.warning("Flavor duplication detected for user %s", user_id)

            # 3) Apply foil stamp (in thread)
            final_bytes_io = await asyncio.to_thread(add_foil_stamp_sync, card_image, FOIL_STAMP_PATH)

            # 4) reply
            keyboard = [[InlineKeyboardButton("🎨 Create another RIZO card", callback_data="create_another")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            final_bytes_io.seek(0)
            await update.message.reply_photo(photo=final_bytes_io, caption="Here’s your RIZO card! 🃏", reply_markup=reply_markup)

    except Exception as exc:
        log.exception("Error generating card: %s", exc)
        await update.message.reply_text(f"Sorry, something went wrong: {exc}")

    finally:
        # allow next generation and refresh cooldown timestamp
        context.user_data["can_generate"] = True
        user_last_request[user_id] = datetime.utcnow()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if query.data == "create_another":
        context.user_data["can_generate"] = True
        await query.message.reply_text("Awesome! Send me a new meme image, and I'll make another RIZO card for you.")

# -----------------------------
# FastAPI + PTB setup
# -----------------------------
fastapi_app = FastAPI()
ptb_app = ApplicationBuilder().token(BOT_TOKEN).build()

# Register handlers
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("generate", generate_cmd))
ptb_app.add_handler(MessageHandler(filters.PHOTO, handle_image))
ptb_app.add_handler(CallbackQueryHandler(button_callback))

@fastapi_app.on_event("startup")
async def startup_event():
    log.info("Starting Telegram application...")
    await ptb_app.initialize()
    await ptb_app.start()
    # set webhook to external URL
    try:
        await ptb_app.bot.set_webhook(WEBHOOK_URL)
        log.info("Webhook set to %s", WEBHOOK_URL)
    except Exception as exc:
        log.exception("Failed to set webhook: %s", exc)
        # Do not raise here — letting app start may be desirable; uncomment to fail startup:
        # raise

@fastapi_app.on_event("shutdown")
async def shutdown_event():
    log.info("Stopping Telegram application...")
    try:
        await ptb_app.stop()
        await ptb_app.shutdown()
    except Exception as exc:
        log.exception("Error during shutdown: %s", exc)

@fastapi_app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, ptb_app.bot)
    # hand off to the PTB queue
    await ptb_app.update_queue.put(update)
    return {"ok": True}

@fastapi_app.get("/health")
async def health_check():
    return {"status": "ok"}

# If you want to run locally via `python app.py`, provide an entrypoint
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:fastapi_app", host="0.0.0.0", port=PORT, reload=False)
