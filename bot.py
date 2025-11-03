import os
import logging
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction
from io import BytesIO

# ---------------------------------------------------
# CONFIG
# ---------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEYS = os.getenv("OPENAI_API_KEYS", "").split(",")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN environment variable")

# ---------------------------------------------------
# LOGGING
# ---------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("rizo-bot")

# ---------------------------------------------------
# FASTAPI SETUP
# ---------------------------------------------------
app = FastAPI()

# ---------------------------------------------------
# TELEGRAM SETUP
# ---------------------------------------------------
telegram_app = Application.builder().token(BOT_TOKEN).build()

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

# ---------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to the RIZO Card Bot!\n\nSend a meme image and I’ll turn it into a RIZO trading card!"
    )

async def generate_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("📸 Please send an image to generate a RIZO card.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Get highest resolution image
    photo_file = await update.message.photo[-1].get_file()
    img_bytes = await photo_file.download_as_bytearray()

    await update.message.reply_text("✨ Generating your RIZO card... please wait a moment.")

    # Simulate AI generation here
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Replace with actual OpenAI Image API call if desired
            # response = await client.post(...)
            await update.message.reply_text("✅ Your RIZO card is ready! (AI generation placeholder)")
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        await update.message.reply_text("❌ Something went wrong while generating your card.")

# ---------------------------------------------------
# REGISTER HANDLERS
# ---------------------------------------------------
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.PHOTO, generate_card))

# ---------------------------------------------------
# FASTAPI ENDPOINTS
# ---------------------------------------------------
@app.get("/", response_class=PlainTextResponse)
async def home():
    return "RIZO Card Bot is live 🎴"

@app.post(f"/{BOT_TOKEN}")
async def telegram_webhook(request: Request):
    """Main webhook endpoint."""
    try:
        update = Update.de_json(await request.json(), telegram_app.bot)
        if not telegram_app._initialized:
            await telegram_app.initialize()
        await telegram_app.process_update(update)
        return PlainTextResponse("ok")
    except Exception as e:
        logger.error(f"Error handling webhook: {e}")
        return PlainTextResponse("error", status_code=500)

# ---------------------------------------------------
# WEBHOOK SETUP ROUTE
# ---------------------------------------------------
@app.on_event("startup")
async def set_webhook():
    webhook_url = f"{RENDER_EXTERNAL_URL}/{BOT_TOKEN}"
    async with telegram_app:
        await telegram_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook set to {webhook_url}")
