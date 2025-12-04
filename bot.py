import os
import logging
import json
from datetime import datetime
from typing import Dict, List

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
NAME, AMOUNT, DATE, CATEGORY = range(4)

class GoogleSheetManager:
    def __init__(self):
        logger.info("Initializing Google Sheets...")
        
        # Get credentials from environment variables
        creds_json = os.getenv('GOOGLE_CREDS_JSON')
        sheet_url = os.getenv('SHEET_URL')
        
        logger.info(f"Sheet URL exists: {bool(sheet_url)}")
        logger.info(f"Creds JSON exists: {bool(creds_json)}")
        
        if not creds_json:
            raise ValueError("GOOGLE_CREDS_JSON environment variable is missing")
        
        if not sheet_url:
            raise ValueError("SHEET_URL environment variable is missing")
        
        try:
            # Try to parse the JSON to validate it
            creds_dict = json.loads(creds_json)
            logger.info("Creds JSON parsed successfully")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse GOOGLE_CREDS_JSON: {e}")
            raise ValueError(f"GOOGLE_CREDS_JSON is not valid JSON: {e}")
        
        SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
                  'https://www.googleapis.com/auth/drive']
        
        try:
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            self.client = gspread.authorize(creds)
            logger.info("Google Sheets authorized successfully")
            
            # Try to open the sheet
            self.sheet = self.client.open_by_url(sheet_url).sheet1
            logger.info("Google Sheet opened successfully")
            
        except Exception as e:
            logger.error(f"Failed to authorize or open sheet: {e}")
            raise
        
        # Initialize headers if sheet is empty
        if not self.sheet.get_all_values():
            self.sheet.append_row([
                'Timestamp', 'User ID', 'Name', 'Amount', 
                'Date', 'Category', 'Description'
            ])
            logger.info("Initialized sheet headers")
    
    def add_transaction(self, data: Dict):
        """Add a new transaction to the sheet"""
        row = [
            datetime.now().isoformat(),
            data.get('user_id'),
            data.get('name'),
            data.get('amount'),
            data.get('date'),
            data.get('category'),
            data.get('description', '')
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
    def __init__(self, sheet_manager: GoogleSheetManager):
        self.sheet = sheet_manager
        
    async def start(self, update: Update, context: CallbackContext):
        """Send welcome message"""
        user = update.effective_user
        welcome_text = f"""
👋 Hello {user.first_name}!

Welcome to Receipt Tracker Bot!

Available commands:
/start - Show this message
/add - Add a new transaction
/search - Search transactions by name
/list - List all people in records
/help - Show help

To add a transaction, use /add command and follow the prompts.
        """
        await update.message.reply_text(welcome_text)
        return ConversationHandler.END
    
    async def add_receipt(self, update: Update, context: CallbackContext):
        """Start transaction addition process"""
        await update.message.reply_text(
            "📝 Please enter the person's name for this transaction:\n"
            "(Type /cancel to abort)"
        )
        return NAME
    
    async def handle_name(self, update: Update, context: CallbackContext):
        """Get person's name"""
        context.user_data['name'] = update.message.text
        
        await update.message.reply_text(
            "💰 Enter the amount (e.g., 25.50):"
        )
        return AMOUNT
    
    async def handle_amount(self, update: Update, context: CallbackContext):
        """Get transaction amount"""
        try:
            amount = float(update.message.text.replace('$', '').replace(',', ''))
            context.user_data['amount'] = amount
        except ValueError:
            await update.message.reply_text("❌ Invalid amount. Please enter a number (e.g., 25.50):")
            return AMOUNT
        
        await update.message.reply_text(
            "📅 Enter the date (YYYY-MM-DD or type 'today'):"
        )
        return DATE
    
    async def handle_date(self, update: Update, context: CallbackContext):
        """Get transaction date"""
        date_text = update.message.text.strip()
        if date_text.lower() == 'today':
            date_text = datetime.now().strftime('%Y-%m-%d')
        
        context.user_data['date'] = date_text
        
        # Show categories
        keyboard = [
            [InlineKeyboardButton("Food 🍔", callback_data="Food")],
            [InlineKeyboardButton("Transport 🚗", callback_data="Transport")],
            [InlineKeyboardButton("Shopping 🛍️", callback_data="Shopping")],
            [InlineKeyboardButton("Entertainment 🎬", callback_data="Entertainment")],
            [InlineKeyboardButton("Utilities 💡", callback_data="Utilities")],
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
        
        category = query.data
        context.user_data['category'] = category
        
        # Save transaction
        transaction_data = {
            'user_id': update.effective_user.id,
            'name': context.user_data.get('name'),
            'amount': context.user_data.get('amount'),
            'date': context.user_data.get('date'),
            'category': context.user_data.get('category'),
            'description': 'Added via bot'
        }
        
        try:
            self.sheet.add_transaction(transaction_data)
            
            await query.edit_message_text(
                f"✅ Transaction saved!\n\n"
                f"Name: {transaction_data['name']}\n"
                f"Amount: ${transaction_data['amount']:.2f}\n"
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
    
    async def cancel(self, update: Update, context: CallbackContext):
        """Cancel the conversation"""
        context.user_data.clear()
        await update.message.reply_text("Operation cancelled.")
        return ConversationHandler.END
    
    async def help_command(self, update: Update, context: CallbackContext):
        """Show help message"""
        help_text = """
📋 **Receipt Tracker Bot Help**

**Commands:**
/start - Start the bot
/add - Add a new transaction manually
/search <name> - Search transactions by name
/list - List all people in records
/help - Show this help message

**How to add a transaction:**
1. Use /add command
2. Enter the person's name
3. Enter the amount
4. Enter the date (or type 'today')
5. Select a category

**Example:**
/search John Doe
Shows all transactions for John Doe

**Example:**
/add
[Follow the prompts to add a transaction]
        """
        await update.message.reply_text(help_text)

async def error_handler(update: Update, context: CallbackContext):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ An error occurred. Please try again or contact support."
        )

def main():
    """Start the bot"""
    # Configuration
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN environment variable not set")
        print("❌ ERROR: TELEGRAM_TOKEN environment variable not set")
        return
    
    print(f"✅ TELEGRAM_TOKEN exists: {bool(TELEGRAM_TOKEN)}")
    print("Initializing Google Sheets...")
    
    # Initialize Google Sheets
    try:
        sheet_manager = GoogleSheetManager()
        logger.info("✅ Google Sheets initialized successfully")
        print("✅ Google Sheets initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Google Sheets: {e}")
        print(f"❌ Failed to initialize Google Sheets: {e}")
        return
    
    # Create bot
    bot = ReceiptBot(sheet_manager)
    
    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add conversation handler for adding receipts
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('add', bot.add_receipt)],
        states={
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
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Start the bot
    logger.info("🤖 Bot is starting...")
    print("🤖 Bot is starting...")
    print("✅ All systems go! Bot is running and ready to receive messages.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
