import os
import logging
import json
import re
import base64
import asyncio
import traceback
from datetime import datetime
from typing import Dict, List, Union

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler, ConversationHandler

# --- Conditional Imports for External Libraries ---
try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False
    print("⚠️ gspread or google-auth not available - Google Sheets features disabled")

try:
    import openai
    OPENAI_AVAILABLE = True
    print("✅ OpenAI available")
except ImportError:
    OPENAI_AVAILABLE = False
    print("⚠️ OpenAI not available - AI features disabled")
    openai = None
# -------------------------------------------------

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
CONFIRM_DETAILS, NAME, AMOUNT, DATE, CATEGORY, DESCRIPTION = range(6)

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
        else:
            logger.warning("OpenAI not available or no API key")
            
    async def analyze_receipt_image(self, image_bytes: bytes) -> Dict[str, any]:
        """Analyze receipt image using GPT-4 Vision"""
        if not self.openai_client or not OPENAI_AVAILABLE:
            logger.warning("OpenAI client not available")
            return {
                "error": "AI features not available",
                "store_name": "Unknown Store",
                "total_amount": 0.00,
                "date": datetime.now().strftime('%Y-%m-%d'),
                "summary": "AI analysis disabled. Please enter details manually."
            }
        
        try:
            # Encode image to base64
            image_b64 = base64.b64encode(image_bytes).decode('utf-8')
            
            # --- The Crucial Prompt ---
            prompt = """Analyze this receipt image and extract the following information in JSON format:
            {
                "store_name": "Name of the store/business",
                "total_amount": 0.00,
                "date": "Date on receipt in YYYY-MM-DD format if available",
                "currency": "USD",
                "summary": "Brief summary of the receipt"
            }

            Rules:
            1. Return ONLY valid JSON, no other text
            2. If information is not available, use null or empty string
            3. Convert amounts to numbers
            4. Date should be in YYYY-MM-DD format if possible"""
            # --------------------------
            
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
                max_tokens=500
            )
            
            content = response.choices[0].message.content
            logger.info(f"OpenAI Response: {content}")
            
            # Robust JSON extraction
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                json_str = json_match.group()
                try:
                    receipt_data = json.loads(json_str)
                    logger.info("✅ Successfully parsed receipt data")
                    return receipt_data
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse JSON: {e}")
                    return {"error": f"JSON parse error: {e}"}
            else:
                logger.error("No JSON found in response")
                return {"error": "No JSON in response"}
                
        except Exception as e:
            logger.error(f"OpenAI Vision error: {e}")
            logger.error(traceback.format_exc())
            return {"error": str(e)}
    
    def format_receipt_for_display(self, receipt_data: Dict) -> str:
        """Format receipt data for user display"""
        if "error" in receipt_data:
            response = "📸 Photo received!\n"
            response += f"AI analysis failed: {receipt_data.get('error', 'Unknown Error')}\n\n"
            response += "Please enter the details manually:\n"
            return response

        response = "🤖 **AI Receipt Analysis:**\n\n"
        
        store = receipt_data.get('store_name')
        response += f"🏪 **Store:** {store or 'Unknown'}\n"
        
        total_amount = receipt_data.get('total_amount')
        if isinstance(total_amount, (int, float)):
            currency = receipt_data.get('currency', 'USD')
            response += f"💰 **Total:** {currency} {total_amount:.2f}\n"
        else:
            response += f"💰 **Total:** Not detected\n"
        
        date_str = receipt_data.get('date')
        response += f"📅 **Date:** {date_str or 'Not detected'}\n"
        
        summary = receipt_data.get('summary')
        if summary:
            response += f"\n📝 **Summary:** {summary}\n"
        
        response += "\nWould you like to save this receipt?"
        return response

class GoogleSheetManager:
    def __init__(self):
        if not GSPREAD_AVAILABLE:
            raise RuntimeError("Google Sheets client libraries are not installed.")

        logger.info("Initializing Google Sheets...")
        creds_json = os.getenv('GOOGLE_CREDS_JSON')
        sheet_url = os.getenv('SHEET_URL')
        
        if not (creds_json and sheet_url):
            raise ValueError("GOOGLE_CREDS_JSON or SHEET_URL environment variable is missing")
        
        try:
            creds_dict = json.loads(creds_json)
        except json.JSONDecodeError:
            logger.error("Invalid JSON in GOOGLE_CREDS_JSON")
            raise

        SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        
        try:
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            self.client = gspread.authorize(creds)
            self.sheet = self.client.open_by_url(sheet_url).sheet1
            logger.info(f"✅ Sheet opened: {self.sheet.title}")
            
            # Initialize headers
            if not self.sheet.get_all_values():
                headers = [
                    'Timestamp', 'User ID', 'Name', 'Amount',  
                    'Date', 'Category', 'Description', 'Store',
                    'AI Analysis', 'Image Available'
                ]
                self.sheet.append_row(headers)
                logger.info("📝 Initialized sheet headers")
        except Exception as e:
            logger.error(f"Failed to initialize Google Sheets: {e}")
            logger.error(traceback.format_exc())
            raise
    
    def add_transaction(self, data: Dict):
        """Add a new transaction to the sheet"""
        row = [
            datetime.now().isoformat(),
            data.get('user_id'),
            data.get('name'),
            f"{data.get('amount', 0.0):.2f}", 
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

class ReceiptBot:
    def __init__(self, sheet_manager: GoogleSheetManager):
        self.sheet = sheet_manager
        self.ai_vision = AIVisionProcessor(os.getenv('OPENAI_API_KEY'))
        
    async def start(self, update: Update, context: CallbackContext):
        """Send welcome message"""
        user = update.effective_user
        # Use saved name Michael, as per personalization request, if the user initiates the chat.
        user_name = "Michael" 
        
        welcome_text = f"""
👋 Hello {user_name}!

Welcome to Receipt Scanner Bot! 📸
I will automatically extract data from your receipt photos and save them to Google Sheets.

**How to use:**
1. Send me a photo of any receipt
2. I'll analyze it (if AI is available)
3. Enter the remaining details (like your name)
4. ✅ Save to Google Sheets!

**Commands:**
/add - Add transaction manually
/search <name> - Find transactions
/list - List all people
/help - Show help

Try sending me a receipt photo now! 📸
        """
        await update.message.reply_text(welcome_text)
        return ConversationHandler.END
    
    async def handle_photo(self, update: Update, context: CallbackContext):
        """Handle receipt photo upload with optional AI analysis"""
        try:
            await update.message.reply_text("🤖 Analyzing receipt with AI...")
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            
            context.user_data.clear() # Clear state before starting new flow
            context.user_data['receipt_photo'] = photo_bytes
            context.user_data['has_image'] = True
            
            receipt_data = await self.ai_vision.analyze_receipt_image(photo_bytes)
            context.user_data['ai_analysis'] = receipt_data
            analysis_display = self.ai_vision.format_receipt_for_display(receipt_data)
            
            if "error" in receipt_data:
                await update.message.reply_text(analysis_display)
                await update.message.reply_text("Please enter the person's name for this receipt:")
                return NAME
            
            # If successful AI analysis, ask for confirmation
            keyboard = [
                [InlineKeyboardButton("✅ Save using AI data", callback_data="save_ai")],
                [InlineKeyboardButton("✏️ Enter manually", callback_data="manual")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(analysis_display, reply_markup=reply_markup, parse_mode='Markdown')
            return CONFIRM_DETAILS
                
        except Exception as e:
            logger.error(f"Error processing photo: {e}")
            await update.message.reply_text(
                "❌ An error occurred while processing the photo. Starting manual entry.\n\n"
                "Please enter the person's name:"
            )
            context.user_data['has_image'] = True
            return NAME
    
    async def handle_confirmation(self, update: Update, context: CallbackContext):
        """Handle user confirmation (save or manual edit) for AI analysis"""
        query = update.callback_query
        await query.answer()
        receipt_data = context.user_data.get('ai_analysis', {})

        if query.data == 'cancel':
            await query.edit_message_text("❌ Receipt cancelled.")
            context.user_data.clear()
            return ConversationHandler.END

        if query.data == 'manual':
            await query.edit_message_text("✏️ Starting manual entry.\n\nPlease enter the person's name:")
            return NAME

        if query.data == 'save_ai':
            amount = receipt_data.get('total_amount')
            store = receipt_data.get('store_name')
            
            if not isinstance(amount, (int, float)) or amount <= 0:
                await query.edit_message_text(
                    "⚠️ The AI couldn't detect a valid total amount.\n"
                    "Please enter the person's name to start manual entry:"
                )
                return NAME

            # Pre-fill data for automatic saving later
            context.user_data['amount'] = amount
            context.user_data['date'] = receipt_data.get('date') or datetime.now().strftime('%Y-%m-%d')
            context.user_data['store'] = store or 'Unknown Store'
            context.user_data['description'] = receipt_data.get('summary') or ''
            
            await query.edit_message_text(
                f"✅ AI data pre-filled! Now, please enter the **person's name** for this receipt:\n"
                f"(Store: {store or 'Unknown'}, Total: ${amount:.2f})"
            )
            return NAME # Start the name collection, then skip to category
    
    async def add_receipt(self, update: Update, context: CallbackContext):
        """Start manual transaction addition"""
        context.user_data.clear()
        context.user_data['has_image'] = False 
        await update.message.reply_text(
            "📝 Starting manual transaction entry.\n\n"
            "Please enter the person's name:\n"
            "(Type /cancel to abort)"
        )
        return NAME
    
    async def handle_name(self, update: Update, context: CallbackContext):
        """Get person's name"""
        context.user_data['name'] = update.message.text.strip()
        
        # Check if amount was pre-filled by 'save_ai'
        if 'amount' in context.user_data and 'store' in context.user_data:
            # Skip AMOUNT and DATE and go straight to CATEGORY
            await update.message.reply_text(f"👤 Name: {context.user_data['name']}. AI data is ready.")
            return await self._send_category_prompt(update)
        
        # Regular flow: check AI data for suggested amount
        receipt_data = context.user_data.get('ai_analysis', {})
        total_amount = receipt_data.get('total_amount')
        
        if isinstance(total_amount, (int, float)) and total_amount > 0 and 'error' not in receipt_data:
            await update.message.reply_text(
                f"💰 AI detected: ${total_amount:.2f}\n"
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
        amount = None
        
        if not user_input and isinstance(detected_amount, (int, float)) and 'error' not in receipt_data:
            amount = detected_amount
        else:
            try:
                amount = float(user_input.replace('$', '').replace(',', '').strip())
                if amount <= 0:
                     raise ValueError("Amount must be positive.")
            except ValueError:
                await update.message.reply_text("❌ Invalid amount. Please enter a positive number (e.g., 25.50):")
                return AMOUNT
        
        context.user_data['amount'] = amount
        return await self._send_date_prompt(update, context)

    async def _send_date_prompt(self, update: Update, context: CallbackContext):
        """Helper to send date prompt"""
        receipt_data = context.user_data.get('ai_analysis', {})
        detected_date = receipt_data.get('date')

        if detected_date and 'error' not in receipt_data:
            await update.message.reply_text(
                f"📅 AI detected: {detected_date}\n"
                "Press Enter to accept, or enter a different date (YYYY-MM-DD or 'today'):"
            )
        else:
            await update.message.reply_text("📅 Enter the date (YYYY-MM-DD or type 'today'):")
            
        return DATE
    
    async def handle_date(self, update: Update, context: CallbackContext):
        """Get transaction date and validate it"""
        user_input = update.message.text.strip()
        receipt_data = context.user_data.get('ai_analysis', {})
        detected_date = receipt_data.get('date')
        date_text = None

        if not user_input and detected_date and 'error' not in receipt_data:
            date_text = detected_date
        elif user_input.lower() == 'today':
            date_text = datetime.now().strftime('%Y-%m-%d')
        else:
            date_text = user_input

        # Validate the date format
        try:
            datetime.strptime(date_text, '%Y-%m-%d')
        except ValueError:
            await update.message.reply_text("❌ Invalid date format. Please use YYYY-MM-DD (e.g., 2025-01-31) or type 'today':")
            return DATE
        
        context.user_data['date'] = date_text
        
        # Store store name from AI analysis if not already pre-filled
        if 'store' not in context.user_data:
            store = receipt_data.get('store_name', '')
            context.user_data['store'] = store if (store and 'error' not in receipt_data) else 'Unknown'
        
        return await self._send_category_prompt(update)
    
    async def _send_category_prompt(self, update: Union[Update, Message]):
        """Helper to send category selection prompt"""
        keyboard = [
            [InlineKeyboardButton("Food 🍔", callback_data="Food"), InlineKeyboardButton("Transport 🚗", callback_data="Transport")],
            [InlineKeyboardButton("Shopping 🛍️", callback_data="Shopping"), InlineKeyboardButton("Entertainment 🎬", callback_data="Entertainment")],
            [InlineKeyboardButton("Utilities 💡", callback_data="Utilities"), InlineKeyboardButton("Medical 🏥", callback_data="Medical")],
            [InlineKeyboardButton("Income 💵", callback_data="Income"), InlineKeyboardButton("Other ❓", callback_data="Other")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("Select a category:", reply_markup=reply_markup)
        else:
            # Fallback for when update is already a Message object (less common)
            await update.reply_text("Select a category:", reply_markup=reply_markup)

        return CATEGORY
    
    async def handle_category(self, update: Update, context: CallbackContext):
        """Handle category selection"""
        query = update.callback_query
        await query.answer()
        
        category = query.data
        context.user_data['category'] = category
        
        description = context.user_data.get('description')
        
        if description and description.strip():
            await query.edit_message_text(f"Category: {category}. Description pre-filled by AI. ✅ Saving transaction...")
            return await self._save_transaction_and_end(update, context) # Save immediately
        
        # If not pre-filled, ask for description
        await query.edit_message_text(
            f"Category: {category}\n\n"
            "Enter description (optional, or type 'skip'):"
        )
        
        return DESCRIPTION
    
    async def handle_description(self, update: Update, context: CallbackContext):
        """Handle description input and save the transaction"""
        
        description = update.message.text.strip()
        if description.lower() != 'skip':
            context.user_data['description'] = description
        else:
            context.user_data['description'] = '' 
        
        return await self._save_transaction_and_end(update.message, context)
    
    async def _save_transaction_and_end(self, update_source: Union[Update, Message], context: CallbackContext):
        """Internal function to finalize and save the transaction"""
        
        # Safely determine the update source for replying
        reply_func = update_source.reply_text if hasattr(update_source, 'reply_text') else lambda msg, **kwargs: update_source.bot.send_message(update_source.effective_chat.id, msg, **kwargs)

        receipt_data = context.user_data.get('ai_analysis', {})
        transaction_data = {
            'user_id': update_source.effective_user.id, 
            'name': context.user_data.get('name'),
            'amount': context.user_data.get('amount'),
            'date': context.user_data.get('date'),
            'category': context.user_data.get('category'),
            'description': context.user_data.get('description', ''),
            'store': context.user_data.get('store', 'Unknown'),
            'ai_analysis': 'ai_analysis' in context.user_data and 'error' not in receipt_data,
            'has_image': context.user_data.get('has_image', False)
        }
        
        if not all([transaction_data['name'], transaction_data['amount'], transaction_data['date'], transaction_data['category']]):
            await reply_func("❌ Error: Essential transaction data is missing. Please start again.")
            context.user_data.clear()
            return ConversationHandler.END

        try:
            self.sheet.add_transaction(transaction_data)
            
            success_msg = f"✅ Saved to Google Sheets!\n\n"
            success_msg += f"👤 Name: {transaction_data['name']}\n"
            success_msg += f"💰 Amount: ${float(transaction_data['amount']):.2f}\n"
            success_msg += f"📅 Date: {transaction_data['date']}\n"
            success_msg += f"📊 Category: {transaction_data['category']}\n"
            success_msg += f"🏪 Store: {transaction_data['store']}\n"
            
            if transaction_data.get('description'):
                success_msg += f"📝 Description: {transaction_data['description']}\n"
            
            if transaction_data['ai_analysis']:
                success_msg += "🤖 AI analyzed\n"
            
            await reply_func(success_msg)
            
        except Exception as e:
            logger.error(f"Error saving: {e}")
            await reply_func("❌ Error saving to Google Sheets. Check the logs and Sheet URL/Permissions.")
            
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
            total = 0.0
            
            for i, transaction in enumerate(transactions, 1):
                try:
                    amount = float(str(transaction.get('Amount', 0.0)))
                except ValueError:
                    amount = 0.0 
                    
                total += amount
                
                response += (
                    f"{i}. Date: {transaction.get('Date', 'N/A')}\n"
                    f"   Amount: ${amount:.2f}\n"
                    f"   Category: {transaction.get('Category', 'N/A')}\n"
                )
                
                if transaction.get('Store', ''):
                    response += f"   Store: {transaction.get('Store')}\n"
                
                if transaction.get('Description', ''):
                    response += f"   Note: {transaction.get('Description')}\n"
                
                response += f"   {'─' * 30}\n"
                
            response += f"\n💰 **Total Expenses:** ${total:.2f}"
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
        if update.callback_query:
             await update.callback_query.edit_message_text("Operation cancelled.")
        else:
            await update.message.reply_text("Operation cancelled.")
        return ConversationHandler.END
    
    async def help_command(self, update: Update, context: CallbackContext):
        """Show help message"""
        # ... (help message is fine)
        await update.message.reply_text("🤖 **Receipt Scanner Bot Help**\n\n**Features:**\n* **📸 Photo Scan:** Send a photo, AI extracts data.\n* **📝 Manual Entry:** Use `/add` to enter details yourself.\n* **📊 Data Tracking:** Transactions are saved to a Google Sheet.\n\n**Commands:**\n/start - Welcome message\n/add - Add transaction manually\n/search <name> - Find transactions for a person\n/list - List all people with records\n/help - This message\n\n**In Conversation:**\n/cancel - Abort the current transaction entry at any time.")

def main():
    """Start the bot"""
    print("🚀 Starting Receipt Scanner Bot...")
    
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN missing. Bot cannot start.")
        return
    
    if not GSPREAD_AVAILABLE:
        print("❌ Google Sheets dependencies missing. Bot cannot save data.")
        return

    try:
        sheet_manager = GoogleSheetManager()
    except Exception as e:
        print(f"❌ Google Sheets failed to initialize. Check environment variables: {e}")
        return
    
    bot = ReceiptBot(sheet_manager)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # --- Conversation Handlers ---
    
    conversation_states = {
        CONFIRM_DETAILS: [CallbackQueryHandler(bot.handle_confirmation), CommandHandler('cancel', bot.cancel)],
        NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_name), CommandHandler('cancel', bot.cancel)],
        AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_amount), CommandHandler('cancel', bot.cancel)],
        DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_date), CommandHandler('cancel', bot.cancel)],
        CATEGORY: [CallbackQueryHandler(bot.handle_category), CommandHandler('cancel', bot.cancel)],
        DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_description), CommandHandler('cancel', bot.cancel)]
    }

    photo_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, bot.handle_photo)],
        states=conversation_states,
        fallbacks=[CommandHandler('cancel', bot.cancel)],
        allow_reentry=True
    )
    
    manual_handler = ConversationHandler(
        entry_points=[CommandHandler('add', bot.add_receipt)],
        states=conversation_states,
        fallbacks=[CommandHandler('cancel', bot.cancel)],
        allow_reentry=True
    )
    
    # --- Add Handlers ---
    application.add_handler(CommandHandler('start', bot.start))
    application.add_handler(photo_handler)
    application.add_handler(manual_handler)
    application.add_handler(CommandHandler('search', bot.search_transactions))
    application.add_handler(CommandHandler('list', bot.list_names))
    application.add_handler(CommandHandler('help', bot.help_command))
    
    # Start bot
    print("🤖 Bot is running...")
    print("📱 Send /start to your bot on Telegram")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
