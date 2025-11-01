import os
import io
import base64
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
import openai

# -----------------------------
# Configuration
# -----------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FOIL_STAMP_PATH = "Assets/Foil_Stamp.png"
WEBHOOK_PATH = "/telegram_webhook"
PORT = int(os.environ.get("PORT", 10000))
DOMAIN = os.getenv("RENDER_EXTERNAL_URL", "https://your-render-domain.com")
WEBHOOK_URL = f"{DOMAIN}{WEBHOOK_PATH}"

openai.api_key = OPENAI_API_KEY

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
- Main art: Use the uploaded meme image as the character art, dynamically styled without changing the underlying character in the meme (remove or ignore the word RIZO or HAHAYES if present in the uploaded image). 
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
def generate_suimon_card(image_bytes_io, prompt_text):
    image_bytes_io.seek(0)
    img = Image.open(image_bytes_io)
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    img_bytes_io = io.BytesIO()
    img.save(img_bytes_io, format="PNG")
    img_bytes_io.name = "meme.png"
    img_bytes_io.seek(0)

    full_prompt = f"Use the uploaded image as the main character for a SUIMON card. {prompt_text}"

    response = openai.images.edit(
        model="gpt-image-1",
        image=img_bytes_io,
        prompt=full_prompt,
        size="1024x1536"
    )

    card_b64 = response.data[0].b64_json
    return Image.open(io.BytesIO(base64.b64decode(card_b64)))


def add_foil_stamp(card_image: Image.Image, logo_path="Assets/Foil_Stamp.png"):
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
        flavor_candidates = [ln for ln in lines if 5 < len(ln) < 80 and "weak" not in ln.lower() and "resist" not in ln.lower()]
        unique_lines = list(dict.fromkeys(flavor_candidates))
        return len(flavor_candidates) == len(unique_lines)
    except Exception:
        return True

# -----------------------------
# Telegram Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to RIZO card creator! Use /generate to start creating a RIZO card 🃏"
    )


async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["can_generate"] = True
    await update.message.reply_text(
        "Send me a meme image, and I’ll craft a unique RIZO card for you 🃏"
    )


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("can_generate", False):
        await update.message.reply_text(
            "⚠️ Please use /generate or click 'Create another RIZO card' before sending an image."
        )
        return

    context.user_data["can_generate"] = False
    await update.message.reply_text("🎨 Generating your RIZO card... please wait a moment!")

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    meme_bytes_io = io.BytesIO()
    await photo_file.download_to_memory(out=meme_bytes_io)
    meme_bytes_io.seek(0)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        card_image = generate_suimon_card(meme_bytes_io, PROMPT_TEMPLATE)

        hp_ok = check_hp_visibility(card_image)
        flavor_ok = check_flavor_text(card_image)
        if not hp_ok:
            print("⚠️ HP text not detected on generated card.")
        if not flavor_ok:
            print("⚠️ Duplicate flavor text detected on generated card.")

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

# Register handlers after ptb_app is created
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("generate", generate))
ptb_app.add_handler(MessageHandler(filters.PHOTO, handle_image))
ptb_app.add_handler(CallbackQueryHandler(button_callback))

@fastapi_app.on_event("startup")
async def startup_event():
    await ptb_app.initialize()
    await ptb_app.start()
    await ptb_app.bot.set_webhook(WEBHOOK_URL)


@fastapi_app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.update_queue.put(update)
    return {"ok": True}

# -----------------------------
# Health Check Endpoint
# -----------------------------
@fastapi_app.get("/health")
async def health_check():
    return {"status": "ok"}
