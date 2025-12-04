import os
import logging
import json
import re
from datetime import datetime
from typing import Dict, List
import traceback

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
        
        if not creds_json:
            raise ValueError("GOOGLE_CREDS_JSON environment variable is missing")
        
        if not sheet_url:
            raise ValueError("SHEET_URL environment variable is missing")
        
        # Ensure sheet_url is a full URL
        if not sheet_url.startswith('http'):
            # If it's just an ID, convert to full URL
            sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_url}"
        
        try:
            # Parse credentials
            creds_dict = json.loads(creds_json)
            logger.info(f"Service account: {creds_dict.get('client_email')}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            raise
        
        SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
                  'https://www.googleapis.com/auth/drive']
        
        try:
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            self.client = gspread.authorize(creds)
            logger.info("✅ Google Sheets authorized")
            
            # Open the sheet
            self.sheet = self.client.open_by_url(sheet_url).sheet1
            logger.info(f"✅ Sheet opened: {self.sheet.title}")
            
        except Exception as e:
            logger.error(f"Failed to open sheet: {e}")
            raise
        
        # Initialize headers if needed
        try:
            if not self.sheet.get_all_values():
                headers = [
                    'Timestamp', 'User ID', 'Name', 'Amount', 
                    'Date', 'Category', 'Description', 'Has Image'
                ]
                self.sheet.append_row(headers)
                logger.info("📝 Initialized sheet headers")
        except Exception as e:
            logger.error(f"Failed to init headers: {e}")
    
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
            'Yes' if data.get('has_image') else 'No'
        ]
        self.sheet.append_row(row)
        logger.info(f"Added: {data.get('name')} - ${data.get('amount')}")
        return True
    
    def get_transactions_by_name(self, name: str) -> List[Dict]:
        """Get all transactions for a specific person"""
        all_data = self.sheet.get_all_records()
        transactions = []
        
        for row in all_data:
            if row.get('Name', '').lower() == name.lower():
                transactions.append(row)
        
        logger.info(f"Found {len(transactions)} transactions for {name}")
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

I can help you track expenses and receipts.

**How to use:**
1. Use /add to enter a transaction
2. Or send a receipt photo (I'll store it)
3. Enter details when prompted

**Commands:**
/add - Add new transaction
/search <name> - Find transactions
/list - List all people
/help - Show help

Let's get started! Use /add to add your first receipt.
        """
        await update.message.reply_text(welcome_text)
        return ConversationHandler.END
    
    async def handle_photo(self, update: Update, context: CallbackContext):
        """Handle receipt photo - store that we have an image"""
        context.user_data['has_image'] = True
        await update.message.reply_text(
            "📸 Photo received! I'll store that you have a receipt image.\n"
            "Now let's enter the transaction details.\n\n"
            "Please enter the person's name:"
        )
        return NAME
    
    async def add_receipt(self, update: Update, context: CallbackContext):
        """Start manual transaction addition"""
        await update.message.reply_text(
            "📝 Please enter the person's name for this transaction:\n"
            "(Or send a receipt photo first)\n"
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
            [InlineKeyboardButton("Medical 🏥", callback_data="Medical")],
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
        
        await query.edit_message_text(
            f"Category: {category}\n\n"
            "Enter description (optional, or type 'skip'):"
        )
        
        # Store callback query for later
        context.user_data['callback_query'] = query
        return ConversationHandler.END
    
    async def handle_description(self, update: Update, context: CallbackContext):
        """Handle description input"""
        if update.message:
            description = update.message.text
            if description.lower() != 'skip':
                context.user_data['description'] = description
            else:
                context.user_data['description'] = ''
            
            # Save transaction
            transaction_data = {
                'user_id': update.effective_user.id,
                'name': context.user_data.get('name'),
                'amount': context.user_data.get('amount'),
                'date': context.user_data.get('date'),
                'category': context.user_data.get('category'),
                'description': context.user_data.get('description', ''),
                'has_image': context.user_data.get('has_image', False)
            }
            
            try:
                self.sheet.add_transaction(transaction_data)
                
                # Success message
                success_msg = f"✅ Transaction saved!\n\n"
                success_msg += f"Name: {transaction_data['name']}\n"
                success_msg += f"Amount: ${transaction_data['amount']:.2f}\n"
                success_msg += f"Date: {transaction_data['date']}\n"
                success_msg += f"Category: {transaction_data['category']}\n"
                
                if transaction_data.get('description'):
                    success_msg += f"Description: {transaction_data['description']}\n"
                
                if transaction_data['has_image']:
                    success_msg += "📸 Receipt image noted\n"
                
                success_msg += "\nUse /search to view transactions"
                
                await update.message.reply_text(success_msg)
                
            except Exception as e:
                logger.error(f"Error saving: {e}")
                await update.message.reply_text("❌ Error saving transaction.")
            
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
                "Please provide a name:\nExample: /search John Doe"
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
                )
                
                desc = transaction.get('Description', '')
                if desc:
                    response += f"   Note: {desc}\n"
                
                if transaction.get('Has Image') == 'Yes':
                    response += f"   📸 Has receipt image\n"
                
                response += f"   {'─' * 30}\n"
            
            response += f"\n💰 Total: ${total:.2f}"
            response += f"\n📊 Count: {len(transactions)} transactions"
            
            if len(response) > 4000:
                chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
                for chunk in chunks:
                    await update.message.reply_text(chunk)
            else:
                await update.message.reply_text(response)
                
        except Exception as e:
            logger.error(f"Error fetching: {e}")
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
            logger.error(f"Error listing: {e}")
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
/start - Welcome message
/add - Add new transaction
/search <name> - Find transactions by name
/list - List all people
/help - This message

**How to add a receipt:**
1. Send a receipt photo (optional)
2. Use /add command
3. Enter person's name
4. Enter amount
5. Enter date
6. Select category
7. Add description (optional)

**Example:**
/search John Doe
Shows all John's transactions
        """
        await update.message.reply_text(help_text)

def main():
    """Start the bot"""
    print("🚀 Starting Receipt Tracker Bot...")
    
    # Get token
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN missing")
        return
    
    print("✅ Token found")
    
    # Initialize Google Sheets
    try:
        print("📊 Initializing Google Sheets...")
        sheet_manager = GoogleSheetManager()
        print("✅ Google Sheets ready")
    except Exception as e:
        print(f"❌ Google Sheets failed: {e}")
        return
    
    # Create bot
    bot = ReceiptBot(sheet_manager)
    
    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('add', bot.add_receipt),
            MessageHandler(filters.PHOTO, bot.handle_photo)
        ],
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
        fallbacks=[CommandHandler('cancel', bot.cancel)],
        allow_reentry=True
    )
    
    # Add handlers
    application.add_handler(CommandHandler('start', bot.start))
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('search', bot.search_transactions))
    application.add_handler(CommandHandler('list', bot.list_names))
    application.add_handler(CommandHandler('help', bot.help_command))
    
    # Handle text messages (for description after category selection)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_description))
    
    # Start bot
    print("🤖 Bot is running...")
    print("📱 Send /start to your bot on Telegram")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
