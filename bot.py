import os
import io
import base64
import asyncio
import logging
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
from openai import OpenAI

# -----------------------------
# Configuration
# -----------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEYS = [k.strip() for k in os.getenv("OPENAI_API_KEYS", "").split(",") if k.strip()]
FOIL_STAMP_PATH = "assets/Foil_stamp.png"
WEBHOOK_PATH = "/telegram_webhook"
PORT = int(os.environ.get("PORT", 10000))
DOMAIN = os.getenv("RENDER_EXTERNAL_URL", "https://your-render-domain.com")
WEBHOOK_URL = f"{DOMAIN}{WEBHOOK_PATH}"

MAX_CONCURRENCY = 3
USER_COOLDOWN = 60  # seconds

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rizo-card-bot")

clients = [OpenAI(api_key=k) for k in OPENAI_API_KEYS] or [OpenAI()]
client_index = 0
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
user_last_request = {}

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
# Helper functions
# -----------------------------
async def generate_rizo_card(image_bytes_io: io.BytesIO, prompt_text: str):
    global client_index
    async with semaphore:
        client = clients[client_index]
        client_index = (client_index + 1) % len(clients)

        image_bytes_io.seek(0)
        b64_image = base64.b64encode(image_bytes_io.read()).decode("utf-8")

        full_prompt = f"Use this meme image as the main character for a RIZO card.\n\n{prompt_text}"

        response = client.images.generate(
            model="gpt-image-1",
            prompt=full_prompt,
            size="1024x1536",
            image=[{"b64_json": b64_image, "mime_type": "image/png"}]
        )

        card_b64 = response.data[0].b64_json
        return Image.open(io.BytesIO(base64.b64decode(card_b64)))


def add_foil_stamp(card_image: Image.Image, logo_path="assets/Foil_stamp.png"):
    card = card_image.convert("RGBA")
    logo = Image.open(logo_path).convert("RGBA")

    foil_scale = float(os.getenv("FOIL_SCALE", 0.13))
    foil_x_offset = float(os.getenv("FOIL_X_OFFSET", 0.0))
    foil_y_offset = float(os.getenv("FOIL_Y_OFFSET", 0.0))

    logo_width = int(card.width * foil_scale)
    ratio = logo_width / logo.width
    logo_height = int(logo.height * ratio)
    logo_resized = logo.resize((logo_width, logo_height), Image.LANCZOS)

    pos_x = int(card.width - logo_width + card.width * foil_x_offset)
    pos_y = int(card.height - logo_height + card.height * foil_y_offset)

    card.alpha_composite(logo_resized, dest=(pos_x, pos_y))

    output = io.BytesIO()
    card.save(output, format="PNG")
    output.seek(0)
    return output


# -----------------------------
# Telegram Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to RIZO card creator! Use /generate to create a RIZO meme card 🃏"
    )


async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["can_generate"] = True
    await update.message.reply_text(
        "Send me a RIZO meme image, and I’ll craft a unique RIZO card for you 🃏"
    )


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = asyncio.get_event_loop().time()
    if not context.user_data.get("can_generate", False):
        await update.message.reply_text(
            "⚠️ Please use /generate or tap 'Create another RIZO card' first."
        )
        return

    last_time = user_last_request.get(user_id, 0)
    if now - last_time < USER_COOLDOWN:
        remaining = int(USER_COOLDOWN - (now - last_time))
        await update.message.reply_text(
            f"🕓 Please wait {remaining}s before generating another card."
        )
        return
    user_last_request[user_id] = now
    context.user_data["can_generate"] = False

    await update.message.reply_text("🎨 Generating your RIZO card... please wait!")

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    meme_bytes_io = io.BytesIO()
    await photo_file.download_to_memory(out=meme_bytes_io)
    meme_bytes_io.seek(0)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        card_image = await asyncio.to_thread(generate_rizo_card, meme_bytes_io, PROMPT_TEMPLATE)
        final_card_bytes = add_foil_stamp(card_image, FOIL_STAMP_PATH)

        keyboard = [[InlineKeyboardButton("🎨 Create another RIZO card", callback_data="create_another")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_photo(
            photo=final_card_bytes,
            caption="Here’s your RIZO card! 🃏",
            parse_mode="HTML",
            reply_markup=reply_markup
        )

    except Exception as e:
        log.error(f"Error during card generation: {e}")
        await update.message.reply_text(f"Sorry, something went wrong: {e}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "create_another":
        context.user_data["can_generate"] = True
        await query.message.reply_text(
            "Awesome! Send me a new meme image, and I'll make another RIZO card for you."
        )


# -----------------------------
# FastAPI + PTB Setup
# -----------------------------
fastapi_app = FastAPI()
ptb_app = ApplicationBuilder().token(BOT_TOKEN).build()

ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("generate", generate))
ptb_app.add_handler(MessageHandler(filters.PHOTO, handle_image))
ptb_app.add_handler(CallbackQueryHandler(button_callback))


@fastapi_app.on_event("startup")
async def startup_event():
    await ptb_app.initialize()
    await ptb_app.start()
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
