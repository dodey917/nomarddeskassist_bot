import os
import logging
import io
import json
from datetime import datetime
from typing import Optional, List
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

# Import our modules
# Ensure ai_processor.py contains analyze_receipt and generate_sheet_query
from ai_processor import analyze_receipt, generate_sheet_query 
# Ensure schemas.py contains ReceiptExtraction, QuerySchema, and SearchResponse
from schemas import ReceiptExtraction, QuerySchema, SearchResponse 
import gspread
from google.oauth2.service_account import Credentials

# Load environment variables
load_dotenv()

# --- Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Define the expected Google Sheet headers/order
# IMPORTANT: This must match the order in GoogleSheetsHandler.append_transaction
SHEET_HEADERS = [
    "Date_Sent", 
    "Sender_Name", 
    "Receiver_Name", 
    "Account_Number", 
    "Amount", 
    "Timestamp"
]

class GoogleSheetsHandler:
    # (Content is the same as the previous version)
    def __init__(self):
        self.scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        self.client = None
        self.sheet = None
        self._setup_client()
    
    def _setup_client(self):
        """Setup Google Sheets client using environment variable"""
        try:
            creds_json = os.getenv("GOOGLE_SHEETS_CREDENTIALS")
            if not creds_json:
                raise ValueError("GOOGLE_SHEETS_CREDENTIALS environment variable is required")
            
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=self.scope)
            self.client = gspread.authorize(creds)
            
            spreadsheet_id = os.getenv("SPREADSHEET_ID")
            if not spreadsheet_id:
                raise ValueError("SPREADSHEET_ID environment variable is required")
            
            self.sheet = self.client.open_by_key(spreadsheet_id).sheet1
            
            # Ensure headers are present (optional but recommended)
            if self.sheet.row_values(1) != SHEET_HEADERS:
                 logger.warning("Headers not found. Attempting to set headers.")
                 self.sheet.update([SHEET_HEADERS], 'A1')

            logger.info("✅ Google Sheets client setup successfully")
            
        except Exception as e:
            logger.error(f"❌ Google Sheets setup failed: {e}")
            raise
    
    def append_transaction(self, extracted_data: ReceiptExtraction):
        """Append transaction data to Google Sheet"""
        try:
            # The order here MUST match SHEET_HEADERS
            row_data = [
                extracted_data.date_sent,
                extracted_data.sender_name,
                extracted_data.receiver_name,
                extracted_data.account_number,
                f"{extracted_data.amount:.2f}", 
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")  
            ]
            
            self.sheet.append_row(row_data, value_input_option='USER_ENTERED')
            logger.info("✅ Successfully appended transaction to Google Sheets")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to append to Google Sheets: {e}")
            return False

    def search_transactions(self, query: QuerySchema) -> List[SearchResponse]:
        """Searches the Google Sheet based on a structured query."""
        try:
            col_index = None
            for i, header in enumerate(SHEET_HEADERS):
                if header.lower() == query.column_to_search.lower():
                    col_index = i + 1
                    break
            
            if col_index is None:
                logger.error(f"Invalid column search name: {query.column_to_search}")
                return []
            
            matching_cells = self.sheet.findall(query.search_value, in_column=col_index)

            results: List[SearchResponse] = []
            seen_rows = set() 
            
            for cell in matching_cells:
                if cell.row == 1 or cell.row in seen_rows:
                    continue
                
                row_data = self.sheet.row_values(cell.row)
                
                if len(row_data) == len(SHEET_HEADERS):
                    data_dict = dict(zip(SHEET_HEADERS, row_data))
                    
                    results.append(
                        SearchResponse(
                            date_sent=data_dict.get('Date_Sent', 'N/A'),
                            sender_name=data_dict.get('Sender_Name', 'N/A'),
                            receiver_name=data_dict.get('Receiver_Name', 'N/A'),
                            account_number=data_dict.get('Account_Number', 'N/A'),
                            amount=data_dict.get('Amount', 'N/A'),
                            timestamp=data_dict.get('Timestamp', 'N/A')
                        )
                    )
                    seen_rows.add(cell.row)
            
            logger.info(f"✅ Found {len(results)} transactions for query: {query.search_value} in {query.column_to_search}")
            return results
        
        except Exception as e:
            logger.error(f"❌ Failed to search Google Sheets: {e}")
            return []

# Initialize Google Sheets handler globally
sheets_handler = GoogleSheetsHandler()

# --- Telegram Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when command /start is issued."""
    welcome_text = """
🤖 **Advanced Receipt Processing Bot**

I can extract structured data from your receipt images using AI and retrieve data from the database.

**Features:**
1.  **Image Upload:** Send a clear image of a receipt, and I will **automatically** extract and save the details to your Google Sheet.
2.  **Data Query:** Send a text message like "What was the amount received by Michael?" or "Show me all transactions on 2025-12-01" to pull information from the sheet.

Send me a receipt image or a query to get started!
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles incoming receipt images. 
    This function performs AI extraction and automatic GSheet saving.
    """
    user = update.message.from_user
    logger.info(f"📸 Image received from user {user.id}")
    
    # Check if the message is only a photo (not a photo with a caption that might trigger handle_text)
    if update.message.caption and not filters.TEXT.check(update.message):
        # We process it as an image, but the text query logic (handle_text) usually takes care of captions.
        # This check is just a safety measure.
        pass
        
    processing_msg = await update.message.reply_text("🔍 Scanning receipt with AI... Please wait.")

    try:
        # 1. Download the photo (Highest resolution)
        photo_file = await update.message.photo[-1].get_file()
        
        # Download to memory (BytesIO)
        image_stream = io.BytesIO()
        await photo_file.download_to_memory(out=image_stream)
        image_bytes = image_stream.getvalue()

        # 2. Send to GPT-4o for extraction
        extracted_data = analyze_receipt(image_bytes)

        if not extracted_data:
            await processing_msg.edit_text("❌ Could not process image. Please try again with a clearer image or ensure it's a valid receipt.")
            return

        # 3. Save to Google Sheet
        save_success = sheets_handler.append_transaction(extracted_data)
        
        # 4. Reply to User
        if save_success:
            response_msg = (
                f"✅ **Receipt Processed Successfully!**\n\n"
                f"📅 **Date:** {extracted_data.date_sent}\n"
                f"👤 **Sender:** {extracted_data.sender_name}\n"
                f"📥 **Receiver:** {extracted_data.receiver_name}\n"
                f"🔢 **Account:** {extracted_data.account_number}\n"
                f"💰 **Amount:** ${extracted_data.amount:,.2f}\n\n"
                f"💾 **Data saved to Google Sheet**"
            )
            await processing_msg.edit_text(response_msg, parse_mode='Markdown')
            logger.info(f"✅ Receipt processed for user {user.id}")
        else:
             await processing_msg.edit_text("✅ Data extracted but **failed to save** to Google Sheet. Please check your credentials/sheet ID.")

    except Exception as e:
        logger.error(f"❌ Error processing image: {e}")
        await processing_msg.edit_text("⚠️ An unexpected error occurred while processing the receipt. Please try again.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles text input, converts it to a structured query 
    using AI, and searches the Google Sheet.
    """
    user_prompt = update.message.text
    user = update.message.from_user
    
    # Crucial check: If a photo was sent with a caption, handle_image likely ran already. 
    # This prevents the bot from treating the caption as a search query immediately after receipt processing.
    if update.message.photo:
        return # Ignore text handling if a photo was just processed

    logger.info(f"📝 Text query received from user {user.id}: '{user_prompt}'")
    
    search_msg = await update.message.reply_text(f"🧠 Analyzing your query: *{user_prompt}*...", parse_mode='Markdown')

    try:
        # 1. Use AI to generate a structured query
        structured_query: Optional[QuerySchema] = generate_sheet_query(user_prompt)

        if not structured_query:
            await search_msg.edit_text("❌ I couldn't understand your request or generate a valid search query. Please try phrasing it like: 'What was the transaction for Arewa Michael?'")
            return

        # 2. Search the Google Sheet
        results: List[SearchResponse] = sheets_handler.search_transactions(structured_query)

        # 3. Format and Reply
        if not results:
            response_text = f"❌ No transactions found matching *{structured_query.search_value}* in the '{structured_query.column_to_search.title()}' column."
        else:
            response_parts = [
                f"✅ **Found {len(results)} Transaction(s) for '{structured_query.search_value}'**\n"
            ]
            
            for i, transaction in enumerate(results[:5]):
                response_parts.append(
                    f"---\n"
                    f"**Transaction {i+1}:**\n"
                    f"📅 **Date:** {transaction.date_sent}\n"
                    f"👤 **Sender:** {transaction.sender_name}\n"
                    f"📥 **Receiver:** {transaction.receiver_name}\n"
                    f"💰 **Amount:** ${float(transaction.amount):,.2f}"
                )
            
            if len(results) > 5:
                 response_parts.append(f"\n...and {len(results) - 5} more results (showing first 5).")
            
            response_text = "\n".join(response_parts)
            logger.info(f"✅ Successfully pulled {len(results)} results for user {user.id}")

        await search_msg.edit_text(response_text, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"❌ Error handling text query: {e}")
        await search_msg.edit_text("⚠️ An unexpected error occurred while processing your request. Please try again.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors in telegram bot."""
    logger.error(f"Update {update} caused error {context.error}")

def main():
    """Start the bot"""
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    
    if not TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN environment variable is required")
        return
        
    if not OPENAI_API_KEY:
        logger.error("❌ OPENAI_API_KEY environment variable is required")
        return

    try:
        application = Application.builder().token(TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start_command))
        
        # 1. Handler for images (Receipt upload) - **This is the handler that should run for your images.**
        application.add_handler(MessageHandler(filters.PHOTO, handle_image))
        
        # 2. Handler for text (Data query) - This handles all text that IS NOT a command.
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
        
        application.add_error_handler(error_handler)
        
        logger.info("🤖 Bot is starting...")
        print("✅ Bot is running on Render...")
        
        # Start polling
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"❌ Failed to start bot: {e}")

if __name__ == '__main__':
    main()
