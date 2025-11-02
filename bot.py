import os
import io
import base64
import asyncio
import random
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
from openai import OpenAI, OpenAIError  # ✅ Correct import for v1.x SDK

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rizo-bot")

# -----------------------------
# Configuration
# -----------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_KEYS = os.getenv("OPENAI_KEYS", "").split(",")  # ✅ Multiple keys
FOIL_STAMP_PATH = "assets/Foil_stamp.png"
WEBHOOK_PATH = "/telegram_webhook"
PORT = int(os.environ.get("PORT", 10000))
DOMAIN = os.getenv("RENDER_EXTERNAL_URL", "https://your-render-domain.com")
WEBHOOK_URL = f"{DOMAIN}{WEBHOOK_PATH}"
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", 3))
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# -----------------------------
# OpenAI Client Utility
# -----------------------------
def get_openai_client():
    """Pick a random API key for load balancing"""
    valid_keys = [k.strip() for k in OPENAI_KEYS if k.strip()]
    if not valid_keys:
        raise ValueError("No valid OpenAI API keys found in environment variable OPENAI_KEYS")
    return OpenAI(api_key=random.choice(valid_keys))

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

# Semaphore to limit active concurrent image generations
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# Track per-user cooldowns to avoid spam in groups
user_last_request = {}  # user_id -> datetime

# -----------------------------
# OpenAI helper: per-request client with rotation + retry
# -----------------------------
def pick_key(exclude_keys=None):
    """Pick a random key, optionally excluding some that recently failed."""
    candidates = [k for k in OPENAI_KEYS if not (exclude_keys and k in exclude_keys)]
    if not candidates:
        # if all excluded, fallback to full list (last resort)
        return random.choice(OPENAI_KEYS)
    return random.choice(candidates)

def create_openai_client_for_key(key: str):
    """Return an OpenAI client bound to a single API key."""
    return OpenAI(api_key=key)

async def openai_images_edit_with_retries(image_file_io: io.BytesIO, prompt_text: str, size="1024x1536"):
    """
    Attempt to call the Images Edit endpoint with rotation and simple retry.
    Returns a PIL.Image on success.
    Raises last exception on failure.
    """
    tried_keys = set()
    last_exc = None

    for attempt in range(RETRY_ATTEMPTS + 1):
        key = pick_key(exclude_keys=tried_keys)
        tried_keys.add(key)
        client = create_openai_client_for_key(key)

        # ensure bytesio at start
        image_file_io.seek(0)
        try:
            # Run blocking call in thread to keep this function non-blocking
            def call_api():
                # The client.images.edit method expects files/bytes in the older style; this mirrors your existing call.
                # If your SDK expects a slightly different signature, adjust accordingly.
                return client.images.edit(
                    model="gpt-image-1",
                    image=image_file_io,
                    prompt=prompt_text,
                    size=size
                )

            response = await asyncio.to_thread(call_api)

            # Extract base64 and return PIL Image
            card_b64 = response.data[0].b64_json
            return Image.open(io.BytesIO(base64.b64decode(card_b64)))

        except Exception as exc:
            last_exc = exc
            # Log & decide whether to try another key
            msg = f"OpenAI call failed with key[{key[:8]}...]: {type(exc).__name__}: {exc}"
            log.warning(msg)

            # If it's clearly a permissions/401 or invalid key, try next key
            # For other transient errors (rate limit / 429) we also retry with another key
            # If attempt < RETRY_ATTEMPTS continue, with small backoff
            backoff = 1 + attempt * 2
            await asyncio.sleep(backoff)

    # If we get here, all attempts failed
    raise last_exc

# -----------------------------
# Image generation pipeline (synchronous helpers run inside threads)
# -----------------------------
def generate_rizo_card_sync(image_bytes_io, prompt_text):
    """
    Blocking version that calls OpenAI and returns a PIL.Image.
    This function is intended to be called inside asyncio.to_thread.
    """
    # Use the async helper to call images with retries — but it's async; call synchronously via asyncio.run?
    # Instead, move logic so top-level async uses openai_images_edit_with_retries.
    # Keep this for compatibility; not used directly.
    raise RuntimeError("generate_rizo_card_sync should not be used; use openai_images_edit_with_retries from async context.")

def add_foil_stamp_sync(card_image: Image.Image, logo_path=FOIL_STAMP_PATH):
    """Blocking: add foil stamp to card and return BytesIO"""
    card = card_image.convert("RGBA")
    logo = Image.open(logo_path).convert("RGBA")

    logo_width = int(card.width * FOIL_SCALE)
    ratio = logo_width / logo.width
    logo_height = int(logo.height * ratio)
    logo_resized = logo.resize((logo_width, logo_height), Image.LANCZOS)

    pos_x = int(card.width - logo_width + card.width * FOIL_X_OFFSET)
    pos_y = int(card.height - logo_height + card.height * FOIL_Y_OFFSET)

    # Use alpha_composite but paste with mask is safer for different PIL builds
    tmp = Image.new("RGBA", card.size)
    tmp.paste(logo_resized, (pos_x, pos_y), logo_resized)
    card = Image.alpha_composite(card, tmp)

    output = io.BytesIO()
    card.save(output, format="PNG")
    output.seek(0)
    return output

def check_hp_visibility_sync(card_image: Image.Image):
    try:
        text = pytesseract.image_to_string(card_image)
        return "HP" in text.upper()
    except Exception as exc:
        log.warning(f"OCR HP check failed: {exc}")
        return False

def check_flavor_text_sync(card_image: Image.Image):
    try:
        text = pytesseract.image_to_string(card_image)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        flavor_candidates = [ln for ln in lines if 5 < len(ln) < 80 and "weak" not in ln.lower() and "resist" not in ln.lower()]
        unique_lines = list(dict.fromkeys(flavor_candidates))
        return len(flavor_candidates) == len(unique_lines)
    except Exception as exc:
        log.warning(f"OCR flavor check failed: {exc}")
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
    user_id = update.effective_user.id
    now = datetime.utcnow()

    # Per-user cooldown check
    last = user_last_request.get(user_id)
    if last and (now - last).total_seconds() < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - last).total_seconds())
        await update.message.reply_text(f"⚠️ Please wait {remaining}s before generating another card.")
        return

    if not context.user_data.get("can_generate", False):
        await update.message.reply_text("⚠️ Please use /generate or click 'Create another RIZO card' before sending an image.")
        return

    # mark user as recently requested (prevents duplicate parallel requests per-user)
    user_last_request[user_id] = now
    context.user_data["can_generate"] = False

    await update.message.reply_text("🎨 Generating your RIZO card... hang tight — this may take a few seconds.")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Download image (async)
    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    meme_bytes_io = io.BytesIO()
    await photo_file.download_to_memory(out=meme_bytes_io)
    meme_bytes_io.seek(0)

    # Use semaphore to limit concurrent image generations
    try:
        async with semaphore:
            # 1) Generate card image (non-blocking to event loop because API call uses to_thread)
            # We call the async helper that does retries and uses to_thread internally for network
            card_image = await openai_images_edit_with_retries(meme_bytes_io, PROMPT_TEMPLATE, size="1024x1536")

            # 2) Run OCR checks in threads concurrently
            hp_task = asyncio.to_thread(check_hp_visibility_sync, card_image)
            flavor_task = asyncio.to_thread(check_flavor_text_sync, card_image)
            hp_ok, flavor_ok = await asyncio.gather(hp_task, flavor_task)

            if not hp_ok:
                log.warning("HP text not detected on generated card.")
            if not flavor_ok:
                log.warning("Duplicate flavor text detected on generated card.")

            # 3) Add foil stamp in thread
            final_card_bytes = await asyncio.to_thread(add_foil_stamp_sync, card_image, FOIL_STAMP_PATH)

            # 4) Reply with final image (BytesIO works)
            keyboard = [[InlineKeyboardButton("🎨 Create another RIZO card", callback_data="create_another")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            final_card_bytes.seek(0)
            await update.message.reply_photo(
                photo=final_card_bytes,
                caption="Here’s your RIZO card! 🃏",
                parse_mode="HTML",
                reply_markup=reply_markup
            )

    except Exception as exc:
        log.exception("Failed to generate card")
        await update.message.reply_text(f"Sorry, something went wrong: {exc}")

    finally:
        # Allow user to request again
        context.user_data["can_generate"] = True
        # update last request time (keeps cooldown)
        user_last_request[user_id] = datetime.utcnow()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "create_another":
        context.user_data["can_generate"] = True
        await query.message.reply_text("Awesome! Send me a new meme image, and I'll make another RIZO card for you.")

# -----------------------------
# FastAPI + PTB Setup
# -----------------------------
fastapi_app = FastAPI()
ptb_app = ApplicationBuilder().token(BOT_TOKEN).build()

ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("generate", generate_cmd))
ptb_app.add_handler(MessageHandler(filters.PHOTO, handle_image))
ptb_app.add_handler(CallbackQueryHandler(button_callback))

@fastapi_app.on_event("startup")
async def startup_event():
    await ptb_app.initialize()
    await ptb_app.start()
    # set webhook
    await ptb_app.bot.set_webhook(WEBHOOK_URL)
    log.info("Bot started and webhook set to %s", WEBHOOK_URL)

@fastapi_app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.update_queue.put(update)
    return {"ok": True}

@fastapi_app.get("/health")
async def health_check():
    return {"status": "ok"}
