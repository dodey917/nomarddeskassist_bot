import os
import time
import logging
from dotenv import load_dotenv

# Import Telegram and OpenAI Libraries
import openai
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
    CallbackContext
)
from telegram.error import Conflict

# --- 1. CONFIGURATION AND INITIALIZATION ---

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables (for local testing)
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Corrected OpenAI Initialization (Fixed 'proxies' argument error)
def initialize_openai_client():
    """Initializes the OpenAI client, excluding the incompatible 'proxies' argument."""
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("❌ OPENAI_API_KEY is not set in environment variables.")
        return None
        
    try:
        # The openai.OpenAI client should only be initialized with the API key.
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        logger.info("✅ OpenAI client initialized successfully.")
        return client
    except Exception as e:
        # Note: The original 'proxies' error should be fixed by now, 
        # but this logs any other initialization error.
        logger.warning(f"❌ OpenAI client initialization failed: {e}")
        return None

openai_client = initialize_openai_client()

# Placeholder for Google Sheets (replace with your actual initialization)
def initialize_google_sheets():
    logger.info("Initializing Google Sheets...")
    # NOTE: You need to replace this with your actual gspread/google-auth logic
    # try:
    #     gc = gspread.service_account(filename='credentials.json')
    #     spreadsheet = gc.open_by_key(os.getenv("SPREADSHEET_ID"))
    #     logger.info(f"✅ Google Sheet opened: {spreadsheet.title}")
    #     return spreadsheet.worksheet("Sheet1") # Replace with your sheet name
    # except Exception as e:
    #     logger.warning(f"❌ Google Sheets initialization failed: {e}")
    #     return None
    return None # Placeholder
    
worksheet = initialize_google_sheets()

# --- 2. CONVERSATION STATES (Placeholders for your existing logic) ---

# Define states for ConversationHandler (these should match your code)
PHOTO, MANUAL_PROMPT, CONFIRM_ENTRY = range(3)
MANUAL_INPUT = 0 # Example state for manual conversation

# --- 3. BOT HANDLER FUNCTIONS (Placeholders) ---

async def start(update: Update, context: CallbackContext) -> int:
    """Sends a welcome message and starts the receipt processing workflow."""
    await update.message.reply_text("Hello! I am your receipt assistant. Please send a receipt photo or use /manual to enter details.")
    return PHOTO

async def handle_receipt_photo(update: Update, context: CallbackContext) -> int:
    """Handles the photo submission (where you call OpenAI OCR/vision)."""
    await update.message.reply_text("Processing your photo...")
    # Add your logic here to process the image and extract data
    # ...
    await update.message.reply_text("Photo processed. Ready to confirm/save.")
    return ConversationHandler.END # Or CONFIRM_ENTRY

async def handle_manual_input(update: Update, context: CallbackContext) -> int:
    """Enters the manual data entry flow."""
    await update.message.reply_text("Please enter the receipt details manually (e.g., 'Date: 2025-12-12, Amount: 15.50, Description: Coffee').")
    return MANUAL_INPUT

async def manual_prompt(update: Update, context: CallbackContext) -> int:
    """Processes the manual text input."""
    data = update.message.text
    # Add your logic here to parse and save the manual data
    # ...
    await update.message.reply_text(f"Received manual data: {data}. Saved successfully.")
    return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancels the current conversation."""
    await update.message.reply_text('Operation cancelled.')
    return ConversationHandler.END

# --- 4. MAIN FUNCTION AND DEPLOYMENT FIX ---

def main() -> None:
    """Starts the bot using long polling, with a fix for Render deployment conflicts."""
    if not TELEGRAM_TOKEN:
        logger.error("🚫 TELEGRAM_TOKEN is not set. Exiting.")
        return

    # Create the Application and pass your bot's token.
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # --- Add Handlers ---
    
    # 1. Photo Receipt Handler
    photo_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.PHOTO, handle_receipt_photo)],
        states={
            PHOTO: [MessageHandler(filters.PHOTO, handle_receipt_photo)],
            # Add other states like CONFIRM_ENTRY if needed
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False, # Use per_message=False for complex flows
    )
    
    # 2. Manual Entry Handler
    manual_handler = ConversationHandler(
        entry_points=[CommandHandler("manual", handle_manual_input)],
        states={
            MANUAL_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_prompt)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    # Register handlers
    application.add_handler(photo_handler)
    application.add_handler(manual_handler)
    
    # --- RENDER DEPLOYMENT FIX (Robust Polling) ---
    
    logger.info("🗑️ Attempting to clear old webhook and pending updates...")
    try:
        # Delete any prior webhook and drop pending updates from old, failed sessions.
        # This is a critical step to mitigate the 409 Conflict error on deployment.
        application.bot.delete_webhook(drop_pending_updates=True) 
        logger.info("✅ Cleared old webhook and dropped pending updates.")
    except Exception as e:
        # Log this but don't crash the bot
        logger.warning(f"⚠️ Failed to clear webhook/updates. Proceeding anyway: {e}")

    # Introduce a small delay to allow Render's 'old' process to terminate fully
    logger.info("⏳ Waiting 5 seconds to ensure old Render instances are terminated...")
    time.sleep(5) 
        
    print("🤖 Bot is starting long polling...")
    # Start the Bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
