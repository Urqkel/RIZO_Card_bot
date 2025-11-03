import os
import io
import random
import logging
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import (
    Update,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI
from PIL import Image

# ----------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://yourapp.onrender.com/telegram_webhook

# Multiple OpenAI API keys for load balancing (comma-separated)
OPENAI_API_KEYS = os.getenv("OPENAI_API_KEYS", os.getenv("OPENAI_API_KEY", "")).split(",")

MAX_CONCURRENCY = 3  # concurrent image generations allowed
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# Logging setup
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rizo-card-bot")

# ----------------------------------------------------
# OpenAI Clients
# ----------------------------------------------------
clients = [AsyncOpenAI(api_key=k.strip()) for k in OPENAI_API_KEYS if k.strip()]
if not clients:
    raise ValueError("❌ No OpenAI API keys found in environment variable OPENAI_API_KEYS or OPENAI_API_KEY.")

# ----------------------------------------------------
# Prompt Template
# ----------------------------------------------------
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

# ----------------------------------------------------
# Telegram Bot Setup
# ----------------------------------------------------
fastapi_app = FastAPI()

application = Application.builder().token(BOT_TOKEN).build()


# ----------------------------------------------------
# Utility: Choose random OpenAI client
# ----------------------------------------------------
def get_random_client() -> AsyncOpenAI:
    return random.choice(clients)


# ----------------------------------------------------
# Telegram Handlers
# ----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎴 Welcome to the RIZO Card Bot!\n\n"
        "Send me an image or meme, and I'll turn it into a RIZO trading card!"
    )


async def generate_rizo_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles image uploads and generates RIZO cards."""
    if not update.message.photo:
        await update.message.reply_text("Please send an image to generate your RIZO card!")
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()

    # Pick random client for load balancing
    client = get_random_client()

    await update.message.reply_text("🎨 Generating your RIZO card... please wait!")

    async with semaphore:
        try:
            # Convert image to acceptable format
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)

            # Generate RIZO Card using GPT-Image-1
            response = await client.images.generate(
                model="gpt-image-1",
                prompt=PROMPT_TEMPLATE,
                image=buf,
                size="1024x1024",
            )

            image_base64 = response.data[0].b64_json
            image_bytes = io.BytesIO(base64.b64decode(image_base64))
            image_bytes.name = "rizo_card.png"

            await update.message.reply_photo(photo=image_bytes, caption="✨ Your RIZO Card is ready!")

        except Exception as e:
            log.exception("Error during card generation: %s", e)
            await update.message.reply_text(f"⚠️ Sorry, something went wrong: {e}")


# ----------------------------------------------------
# Telegram Command Handlers
# ----------------------------------------------------
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.PHOTO, generate_rizo_card))


# ----------------------------------------------------
# FastAPI Webhook Endpoints
# ----------------------------------------------------
@fastapi_app.on_event("startup")
async def startup_event():
    log.info("Bot started and webhook set to %s", WEBHOOK_URL)
    await application.bot.set_webhook(WEBHOOK_URL)


@fastapi_app.post("/telegram_webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return PlainTextResponse("ok")


@fastapi_app.get("/")
async def root():
    return {"status": "ok", "message": "RIZO Card Bot is alive!"}


# ----------------------------------------------------
# Run (for local debug)
# ----------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("bot:fastapi_app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
