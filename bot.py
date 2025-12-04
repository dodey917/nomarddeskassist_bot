import os
import logging
import json
import re
import base64
import asyncio
from datetime import datetime
from typing import Dict, List
import traceback

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
from telegram.ext import ConversationHandler

import gspread
from google.oauth2.service_account import Credentials

# Try to import OpenAI, but make it optional
try:
    import openai
    OPENAI_AVAILABLE = True
    print("✅ OpenAI available")
except ImportError:
    OPENAI_AVAILABLE = False
    print("⚠️ OpenAI not available - AI features disabled")
    openai = None

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
CONFIRM_DETAILS, NAME, AMOUNT, DATE, CATEGORY = range(5)

class AIVisionProcessor:
    """Handles receipt analysis using OpenAI GPT-4 Vision"""
    
    def __init__(self, openai_api_key: str = None):
        self.openai_client = None
        
        if openai_api_key and OPENAI_AVAILABLE:
            try:
                self.openai_client = openai.OpenAI(api_key=openai_api_key)
                logger.info("✅ OpenAI GPT-4 Vision initialized")
            except Exception as e:
                logger.warning(f"OpenAI initialization failed: {e}")
                self.openai_client = None
        else:
            logger.warning("OpenAI not available or no API key")
    
    async def analyze_receipt_image(self, image_bytes: bytes) -> Dict[str, any]:
        """Analyze receipt image using GPT-4 Vision"""
        if not self.openai_client:
            logger.warning("OpenAI client not available")
            return {
                "store_name": "Unknown Store",
                "total_amount": 0.00,
                "date": datetime.now().strftime('%Y-%m-%d'),
                "currency": "USD",
                "summary": "AI analysis not available. Please enter details manually."
            }
        
        try:
            # Encode image to base64
            image_b64 = base64.b64encode(image_bytes).decode('utf-8')
            
            # Prepare the prompt for receipt analysis
            prompt = """Analyze this receipt image and extract:
            1. Store/business name
            2. Total amount paid
            3. Date of purchase
            4. Currency used
            Return as JSON with keys: store_name, total_amount, date, currency"""
            
            # Call OpenAI API
            response = await asyncio.to_thread(
                self.openai_client.chat.completions.create,
                model="gpt-4-vision-preview",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_b64}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=300
            )
            
            # Extract response
            content = response.choices[0].message.content
            logger.info(f"OpenAI Response: {content}")
            
            # Try to parse JSON
            try:
                json_match = re.search(r'\{.*\}', content, re.DOTALL)
                if json_match:
                    json_str = json_match.group()
                    receipt_data = json.loads(json_str)
                    logger.info(f"✅ Successfully parsed receipt data")
                    return receipt_data
                else:
                    # If no JSON, create basic response
                    return {
                        "store_name": "Store from receipt",
                        "total_amount": 0.00,
                        "date": datetime.now().strftime('%Y-%m-%d'),
                        "currency": "USD",
                        "summary": content[:100]  # First 100 chars of response
                    }
            except json.JSONDecodeError:
                # Return basic info if JSON parsing fails
                return {
                    "store_name": "Store from receipt",
                    "total_amount": 0.00,
                    "date": datetime.now().strftime('%Y-%m-%d'),
                    "currency": "USD",
                    "summary": content[:100]
                }
                
        except Exception as e:
            logger.error(f"OpenAI Vision error: {e}")
            return {
                "store_name": "Unknown Store",
                "total_amount": 0.00,
                "date": datetime.now().strftime('%Y-%m-%d'),
                "currency": "USD",
                "summary": f"Error: {str(e)[:50]}"
            }
    
    def format_receipt_for_display(self, receipt_data: Dict) -> str:
        """Format receipt data for user display"""
        response = "🤖 **Receipt Analysis:**\n\n"
        
        if receipt_data.get('store_name'):
            response += f"🏪 **Store:** {receipt_data['store_name']}\n"
        else:
            response += f"🏪 **Store:** Unknown\n"
        
        if receipt_data.get('total_amount'):
            currency = receipt_data.get('currency', 'USD')
            response += f"💰 **Total:** {currency} {receipt_data['total_amount']:.2f}\n"
        else:
            response += f"💰 **Total:** Not detected\n"
        
        if receipt_data.get('date'):
            response += f"📅 **Date:** {receipt_data['date']}\n"
        else:
            response += f"📅 **Date:** Not detected\n"
        
        if receipt_data.get('summary'):
            response += f"\n📝 **Notes:** {receipt_data['summary']}\n"
        
        response += "\nWould you like to save this receipt?"
        return response

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
                    'Date', 'Category', 'Description', 'Store',
                    'AI Analysis', 'Image Available'
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
            data.get('store', 'Unknown'),
            'Yes' if data.get('ai_analysis') else 'No',
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
        self.ai_vision = AIVisionProcessor(os.getenv('OPENAI_API_KEY'))
        
    async def start(self, update: Update, context: CallbackContext):
        """Send welcome message"""
        user = update.effective_user
        
        # Check if OpenAI is available
        ai_status = "✅ AI analysis available" if self.ai_vision.openai_client else "⚠️ AI analysis not available"
        
        welcome_text = f"""
👋 Hello {user.first_name}!

Welcome to Receipt Scanner Bot! 📸

{ai_status}

**How to use:**
1. Send me a receipt photo
2. I'll analyze it (if AI is available)
3. Enter the person's name
4. Confirm details
5. ✅ Save to Google Sheets!

**Commands:**
/add - Add transaction manually
/search <name> - Find transactions
/list - List all people
/help - Show help

Try sending me a receipt photo! 📸
        """
        await update.message.reply_text(welcome_text)
        return ConversationHandler.END
    
    async def handle_photo(self, update: Update, context: CallbackContext):
        """Handle receipt photo upload"""
        try:
            # Download the photo
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            
            # Store in context
            context.user_data['receipt_photo'] = photo_bytes
            context.user_data['has_image'] = True
            
            # Check if AI is available
            if self.ai_vision.openai_client:
                await update.message.reply_text("🤖 Analyzing receipt with AI...")
                receipt_data = await self.ai_vision.analyze_receipt_image(photo_bytes)
                context.user_data['ai_analysis'] = receipt_data
                analysis_display = self.ai_vision.format_receipt_for_display(receipt_data)
                
                # Create confirmation buttons
                keyboard = [
                    [InlineKeyboardButton("✅ Save to Google Sheets", callback_data="save")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(analysis_display, reply_markup=reply_markup)
                return CONFIRM_DETAILS
            else:
                # No AI available
                await update.message.reply_text(
                    "📸 Photo received!\n\n"
                    "Please enter the person's name for this receipt:\n"
                    "(Type /cancel to abort)"
                )
                return NAME
                
        except Exception as e:
            logger.error(f"Error processing photo: {e}")
            await update.message.reply_text(
                "📸 Photo received!\n\n"
                "Please enter the person's name:\n"
                "(Type /cancel to abort)"
            )
            context.user_data['has_image'] = True
            return NAME
    
    async def handle_confirmation(self, update: Update, context: CallbackContext):
        """Handle user confirmation to save receipt"""
        query = update.callback_query
        await query.answer()
        
        if query.data == 'cancel':
            await query.edit_message_text("❌ Receipt cancelled.")
            context.user_data.clear()
            return ConversationHandler.END
        
        # User wants to save
        await query.edit_message_text(
            "✅ I'll save this receipt!\n\n"
            "Please enter the person's name:\n"
            "(Type /cancel to abort)"
        )
        return NAME
    
    async def add_receipt(self, update: Update, context: CallbackContext):
        """Start manual transaction addition"""
        await update.message.reply_text(
            "📝 Please enter the person's name:\n"
            "(Or send a receipt photo)\n"
            "(Type /cancel to abort)"
        )
        return NAME
    
    async def handle_name(self, update: Update, context: CallbackContext):
        """Get person's name"""
        context.user_data['name'] = update.message.text
        
        # Check if we have AI analysis data
        receipt_data = context.user_data.get('ai_analysis', {})
        total_amount = receipt_data.get('total_amount')
        
        if total_amount:
            currency = receipt_data.get('currency', 'USD')
            await update.message.reply_text(
                f"💰 AI detected: {currency} {total_amount:.2f}\n"
                "Press Enter to accept, or enter a different amount:"
            )
        else:
            await update.message.reply_text("💰 Enter the amount (e.g., 25.50):")
        
        return AMOUNT
    
    async def handle_amount(self, update: Update, context: CallbackContext):
        """Get transaction amount"""
        user_input = update.message.text.strip()
        receipt_data = context.user_data.get('ai_analysis', {})
        detected_amount = receipt_data.get('total_amount')
        
        # If user pressed Enter and we have detected amount, use it
        if user_input == '' and detected_amount:
            amount = detected_amount
        else:
            try:
                amount = float(user_input.replace('$', '').replace(',', ''))
            except ValueError:
                await update.message.reply_text("❌ Invalid amount. Please enter a number (e.g., 25.50):")
                return AMOUNT
        
        context.user_data['amount'] = amount
        
        # Check for date from AI analysis
        detected_date = receipt_data.get('date')
        
        if detected_date:
            await update.message.reply_text(
                f"📅 AI detected: {detected_date}\n"
                "Press Enter to accept, or enter a different date (YYYY-MM-DD or 'today'):"
            )
        else:
            await update.message.reply_text("📅 Enter the date (YYYY-MM-DD or type 'today'):")
        
        return DATE
    
    async def handle_date(self, update: Update, context: CallbackContext):
        """Get transaction date"""
        user_input = update.message.text.strip()
        receipt_data = context.user_data.get('ai_analysis', {})
        detected_date = receipt_data.get('date')
        
        # If user pressed Enter and we have detected date, use it
        if user_input == '' and detected_date:
            date_text = detected_date
        elif user_input.lower() == 'today':
            date_text = datetime.now().strftime('%Y-%m-%d')
        else:
            date_text = user_input
        
        context.user_data['date'] = date_text
        
        # Store store name from AI analysis if available
        store = receipt_data.get('store_name', '')
        if store:
            context.user_data['store'] = store
        elif 'store' not in context.user_data:
            context.user_data['store'] = 'Unknown'
        
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
        
        await update.message.reply_text("Select a category:", reply_markup=reply_markup)
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
            
            # Prepare transaction data
            transaction_data = {
                'user_id': update.effective_user.id,
                'name': context.user_data.get('name'),
                'amount': context.user_data.get('amount'),
                'date': context.user_data.get('date'),
                'category': context.user_data.get('category'),
                'description': context.user_data.get('description', ''),
                'store': context.user_data.get('store', 'Unknown'),
                'ai_analysis': 'ai_analysis' in context.user_data,
                'has_image': context.user_data.get('has_image', False)
            }
            
            try:
                # Save to Google Sheets
                self.sheet.add_transaction(transaction_data)
                
                # Success message
                success_msg = f"✅ Saved to Google Sheets!\n\n"
                success_msg += f"👤 Name: {transaction_data['name']}\n"
                success_msg += f"💰 Amount: ${transaction_data['amount']:.2f}\n"
                success_msg += f"📅 Date: {transaction_data['date']}\n"
                success_msg += f"📊 Category: {transaction_data['category']}\n"
                success_msg += f"🏪 Store: {transaction_data['store']}\n"
                
                if transaction_data.get('description'):
                    success_msg += f"📝 Description: {transaction_data['description']}\n"
                
                if transaction_data['ai_analysis']:
                    success_msg += "🤖 AI analyzed\n"
                if transaction_data['has_image']:
                    success_msg += "📸 Has receipt image\n"
                
                await update.message.reply_text(success_msg)
                
            except Exception as e:
                logger.error(f"Error saving: {e}")
                await update.message.reply_text("❌ Error saving to Google Sheets.")
            
            # Clear user data
            context.user_data.clear()
        
        return ConversationHandler.END
    
    async def search_transactions(self, update: Update, context: CallbackContext):
        """Search transactions by name"""
        if context.args:
            name = ' '.join(context.args)
            await self._show_transactions(update, name)
        else:
            await update.message.reply_text("Please provide a name:\nExample: /search John Doe")
    
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
                
                store = transaction.get('Store', '')
                if store and store != 'Unknown':
                    response += f"   Store: {store}\n"
                
                desc = transaction.get('Description', '')
                if desc:
                    response += f"   Note: {desc}\n"
                
                if transaction.get('AI Analysis') == 'Yes':
                    response += f"   🤖 AI analyzed\n"
                
                response += f"   {'─' * 30}\n"
            
            response += f"\n💰 Total: ${total:.2f}"
            response += f"\n📊 Count: {len(transactions)} transactions"
            
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
🤖 **Receipt Scanner Bot Help**

**Commands:**
/start - Welcome message
/add - Add transaction manually
/search <name> - Find transactions
/list - List all people
/help - This message

**How to use:**
1. Send a receipt photo
2. Or use /add for manual entry
3. Enter details when prompted
4. ✅ Saved to Google Sheets!

**Example:**
/search John Doe
        """
        await update.message.reply_text(help_text)

def main():
    """Start the bot"""
    print("🚀 Starting Receipt Scanner Bot...")
    
    # Get tokens
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN missing")
        return
    
    print(f"✅ TELEGRAM_TOKEN found")
    print(f"🤖 OpenAI available: {OPENAI_AVAILABLE}")
    if OPENAI_API_KEY:
        print(f"🔑 OpenAI API Key: {'Set' if OPENAI_API_KEY else 'Not set'}")
    
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
    
    # Conversation handler for photo analysis
    photo_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO, bot.handle_photo)
        ],
        states={
            CONFIRM_DETAILS: [
                CallbackQueryHandler(bot.handle_confirmation),
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
        fallbacks=[CommandHandler('cancel', bot.cancel)],
        allow_reentry=True
    )
    
    # Conversation handler for manual addition
    manual_handler = ConversationHandler(
        entry_points=[
            CommandHandler('add', bot.add_receipt)
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
    application.add_handler(photo_handler)
    application.add_handler(manual_handler)
    application.add_handler(CommandHandler('search', bot.search_transactions))
    application.add_handler(CommandHandler('list', bot.list_names))
    application.add_handler(CommandHandler('help', bot.help_command))
    
    # Handle text messages (for description after category selection)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_description))
    
    # Start bot
    print("🤖 Bot is running...")
    print("📱 Send /start to your bot on Telegram")
    print("📸 Try sending a receipt photo!")
    
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True  # This helps prevent conflict errors
        )
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    main()
