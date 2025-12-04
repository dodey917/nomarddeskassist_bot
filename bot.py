import os
import re
import logging
from datetime import datetime
from typing import Dict, List, Optional
import pytesseract
from PIL import Image
import io
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
from telegram.ext import ConversationHandler

import gspread
from google.oauth2.service_account import Credentials

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
RECEIPT, NAME, AMOUNT, DATE, CATEGORY = range(5)

# Initialize Google Sheets
SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']

class GoogleSheetManager:
    def __init__(self, creds_path: str, sheet_url: str):
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        self.client = gspread.authorize(creds)
        self.sheet = self.client.open_by_url(sheet_url).sheet1
        
        # Initialize headers if sheet is empty
        if not self.sheet.get_all_values():
            self.sheet.append_row([
                'Timestamp', 'User ID', 'Name', 'Amount', 
                'Date', 'Category', 'Description', 'Receipt Text'
            ])
    
    def add_transaction(self, data: Dict):
        """Add a new transaction to the sheet"""
        row = [
            datetime.now().isoformat(),
            data.get('user_id'),
            data.get('name'),
            data.get('amount'),
            data.get('date'),
            data.get('category'),
            data.get('description', ''),
            data.get('receipt_text', '')
        ]
        self.sheet.append_row(row)
        return True
    
    def get_transactions_by_name(self, name: str) -> List[Dict]:
        """Get all transactions for a specific person"""
        all_data = self.sheet.get_all_records()
        transactions = []
        
        for row in all_data:
            if row.get('Name', '').lower() == name.lower():
                transactions.append(row)
        
        return transactions
    
    def get_all_names(self) -> List[str]:
        """Get list of all unique names"""
        all_data = self.sheet.get_all_records()
        names = set()
        for row in all_data:
            name = row.get('Name', '').strip()
            if name:
                names.add(name)
        return list(names)

class ReceiptBot:
    def __init__(self, token: str, sheet_manager: GoogleSheetManager):
        self.token = token
        self.sheet = sheet_manager
        self.user_data = {}
        
    def extract_text_from_image(self, image_bytes: bytes) -> str:
        """Extract text from receipt image using OCR"""
        try:
            image = Image.open(io.BytesIO(image_bytes))
            text = pytesseract.image_to_string(image)
            return text
        except Exception as e:
            logger.error(f"OCR Error: {e}")
            return ""
    
    def parse_receipt_text(self, text: str) -> Dict:
        """Parse extracted text to find relevant information"""
        parsed = {}
        
        # Look for amounts (common patterns)
        amount_patterns = [
            r'total[\s:]*[$€£]?(\d+\.?\d*)',
            r'amount[\s:]*[$€£]?(\d+\.?\d*)',
            r'[$€£](\d+\.?\d*)',
            r'(\d+\.?\d+)[\s]*[$€£]'
        ]
        
        for pattern in amount_patterns:
            matches = re.findall(pattern, text.lower())
            if matches:
                parsed['amount'] = float(matches[-1])
                break
        
        # Look for date
        date_pattern = r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})|(\d{4}[/-]\d{1,2}[/-]\d{1,2})'
        date_match = re.search(date_pattern, text)
        if date_match:
            parsed['date'] = date_match.group()
        
        return parsed
    
    async def start(self, update: Update, context: CallbackContext):
        """Send welcome message"""
        user = update.effective_user
        welcome_text = f"""
👋 Hello {user.first_name}!

Welcome to Receipt Scanner Bot!

Available commands:
/start - Show this message
/add - Add a new receipt
/search - Search transactions by name
/list - List all people in records
/help - Show help

To add a receipt, send a photo or use /add command.
        """
        await update.message.reply_text(welcome_text)
        return ConversationHandler.END
    
    async def add_receipt(self, update: Update, context: CallbackContext):
        """Start receipt addition process"""
        await update.message.reply_text(
            "📸 Please send a photo of the receipt or type /cancel to abort."
        )
        return RECEIPT
    
    async def handle_receipt_photo(self, update: Update, context: CallbackContext):
        """Handle receipt photo upload"""
        user_id = update.effective_user.id
        
        # Download the photo
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        
        # Extract text from receipt
        extracted_text = self.extract_text_from_image(photo_bytes)
        parsed_info = self.parse_receipt_text(extracted_text)
        
        # Store in user context
        context.user_data['receipt_text'] = extracted_text
        context.user_data['parsed_info'] = parsed_info
        
        # Ask for person's name
        await update.message.reply_text(
            "✅ Receipt processed!\n\n"
            f"Extracted amount: ${parsed_info.get('amount', 'Not found')}\n"
            f"Extracted date: {parsed_info.get('date', 'Not found')}\n\n"
            "Please enter the person's name:"
        )
        return NAME
    
    async def handle_name(self, update: Update, context: CallbackContext):
        """Get person's name"""
        context.user_data['name'] = update.message.text
        
        # Ask for amount (pre-filled if detected)
        amount = context.user_data['parsed_info'].get('amount')
        prompt = "Enter the amount:"
        if amount:
            prompt = f"Enter the amount (detected: ${amount}):"
        
        await update.message.reply_text(prompt)
        return AMOUNT
    
    async def handle_amount(self, update: Update, context: CallbackContext):
        """Get transaction amount"""
        try:
            amount = float(update.message.text.replace('$', '').replace(',', ''))
            context.user_data['amount'] = amount
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Please enter a number:")
            return AMOUNT
        
        # Ask for date (pre-filled if detected)
        date = context.user_data['parsed_info'].get('date')
        prompt = "Enter the date (YYYY-MM-DD):"
        if date:
            prompt = f"Enter the date (detected: {date}):"
        
        await update.message.reply_text(prompt)
        return DATE
    
    async def handle_date(self, update: Update, context: CallbackContext):
        """Get transaction date"""
        context.user_data['date'] = update.message.text
        
        # Show categories
        keyboard = [
            [InlineKeyboardButton("Food 🍔", callback_data="Food")],
            [InlineKeyboardButton("Transport 🚗", callback_data="Transport")],
            [InlineKeyboardButton("Shopping 🛍️", callback_data="Shopping")],
            [InlineKeyboardButton("Entertainment 🎬", callback_data="Entertainment")],
            [InlineKeyboardButton("Other ❓", callback_data="Other")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "Select a category:",
            reply_markup=reply_markup
        )
        return CATEGORY
    
    async def handle_category(self, update: Update, context: CallbackContext):
        """Handle category selection"""
        query = update.callback_query
        await query.answer()
        
        context.user_data['category'] = query.data
        
        # Save to Google Sheets
        transaction_data = {
            'user_id': update.effective_user.id,
            'name': context.user_data.get('name'),
            'amount': context.user_data.get('amount'),
            'date': context.user_data.get('date'),
            'category': context.user_data.get('category'),
            'receipt_text': context.user_data.get('receipt_text', '')
        }
        
        try:
            self.sheet.add_transaction(transaction_data)
            
            await query.edit_message_text(
                f"✅ Transaction saved!\n\n"
                f"Name: {transaction_data['name']}\n"
                f"Amount: ${transaction_data['amount']}\n"
                f"Date: {transaction_data['date']}\n"
                f"Category: {transaction_data['category']}"
            )
        except Exception as e:
            logger.error(f"Error saving to sheet: {e}")
            await query.edit_message_text("❌ Error saving transaction. Please try again.")
        
        # Clear user data
        context.user_data.clear()
        return ConversationHandler.END
    
    async def search_transactions(self, update: Update, context: CallbackContext):
        """Search transactions by name"""
        if context.args:
            name = ' '.join(context.args)
            await self._show_transactions(update, name)
        else:
            await update.message.reply_text(
                "Please provide a name to search:\n"
                "Example: /search John Doe"
            )
    
    async def list_names(self, update: Update, context: CallbackContext):
        """List all names in the database"""
        try:
            names = self.sheet.get_all_names()
            if names:
                response = "📋 People in records:\n\n"
                for i, name in enumerate(sorted(names), 1):
                    response += f"{i}. {name}\n"
                
                response += "\nUse /search <name> to see transactions"
            else:
                response = "No records found yet."
            
            await update.message.reply_text(response)
        except Exception as e:
            logger.error(f"Error listing names: {e}")
            await update.message.reply_text("❌ Error accessing database.")
    
    async def _show_transactions(self, update: Update, name: str):
        """Display transactions for a specific person"""
        try:
            transactions = self.sheet.get_transactions_by_name(name)
            
            if not transactions:
                await update.message.reply_text(f"No transactions found for {name}")
                return
            
            response = f"📊 Transactions for {name}:\n\n"
            total = 0
            
            for i, transaction in enumerate(transactions, 1):
                amount = float(transaction.get('Amount', 0))
                total += amount
                
                response += (
                    f"{i}. Date: {transaction.get('Date', 'N/A')}\n"
                    f"   Amount: ${amount:.2f}\n"
                    f"   Category: {transaction.get('Category', 'N/A')}\n"
                    f"   Description: {transaction.get('Description', '')}\n"
                    f"   {'─' * 30}\n"
                )
            
            response += f"\n💰 Total: ${total:.2f}"
            
            # Telegram has message length limit, so split if needed
            if len(response) > 4000:
                chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
                for chunk in chunks:
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(response)
                
        except Exception as e:
            logger.error(f"Error fetching transactions: {e}")
            await update.message.reply_text("❌ Error fetching transactions.")
    
    async def cancel(self, update: Update, context: CallbackContext):
        """Cancel the conversation"""
        context.user_data.clear()
        await update.message.reply_text("Operation cancelled.")
        return ConversationHandler.END
    
    async def help_command(self, update: Update, context: CallbackContext):
        """Show help message"""
        help_text = """
📋 **Receipt Scanner Bot Help**

**Commands:**
/start - Start the bot
/add - Add a new receipt
/search <name> - Search transactions by name
/list - List all people in records
/help - Show this help message

**How to add a receipt:**
1. Use /add command
2. Send a photo of the receipt
3. Enter the person's name
4. Enter the amount
5. Enter the date
6. Select a category

**Example:**
/search John Doe
Shows all transactions for John Doe
        """
        await update.message.reply_text(help_text)

def main():
    """Start the bot"""
    # Configuration
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_BOT_TOKEN')
    GOOGLE_CREDS_PATH = os.getenv('GOOGLE_CREDS_PATH', 'credentials.json')
    SHEET_URL = os.getenv('SHEET_URL', 'YOUR_GOOGLE_SHEET_URL')
    
    # Initialize Google Sheets
    sheet_manager = GoogleSheetManager(GOOGLE_CREDS_PATH, SHEET_URL)
    
    # Create bot
    bot = ReceiptBot(TELEGRAM_TOKEN, sheet_manager)
    
    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add conversation handler for adding receipts
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('add', bot.add_receipt)],
        states={
            RECEIPT: [
                MessageHandler(filters.PHOTO, bot.handle_receipt_photo),
                CommandHandler('cancel', bot.cancel)
            ],
            NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_name),
                CommandHandler('cancel', bot.cancel)
            ],
            AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_amount),
                CommandHandler('cancel', bot.cancel)
            ],
            DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_date),
                CommandHandler('cancel', bot.cancel)
            ],
            CATEGORY: [
                CallbackQueryHandler(bot.handle_category),
                CommandHandler('cancel', bot.cancel)
            ]
        },
        fallbacks=[CommandHandler('cancel', bot.cancel)]
    )
    
    # Add handlers
    application.add_handler(CommandHandler('start', bot.start))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('search', bot.search_transactions))
    application.add_handler(CommandHandler('list', bot.list_names))
    application.add_handler(CommandHandler('help', bot.help_command))
    
    # Start the bot
    print("🤖 Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
