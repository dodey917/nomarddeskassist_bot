import os
import logging
import json
import re
import base64
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
import traceback

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
from telegram.ext import ConversationHandler

import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI

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
        
        if openai_api_key:
            try:
                # Simplified OpenAI client initialization
                self.openai_client = OpenAI(api_key=openai_api_key)
                logger.info("✅ OpenAI GPT-4 Vision initialized")
            except Exception as e:
                logger.warning(f"OpenAI initialization failed: {e}")
                traceback.print_exc()
        else:
            logger.warning("No OpenAI API key provided")
    
    async def analyze_receipt_image(self, image_bytes: bytes) -> Dict[str, any]:
        """Analyze receipt image using GPT-4 Vision"""
        if not self.openai_client:
            logger.warning("OpenAI client not available")
            return {"error": "OpenAI not configured"}
        
        try:
            # Encode image to base64
            image_b64 = base64.b64encode(image_bytes).decode('utf-8')
            
            # Prepare the prompt for receipt analysis
            prompt = """Analyze this receipt image and extract the following information in JSON format:
            {
                "store_name": "Name of the store/business",
                "total_amount": 0.00,
                "date": "Date on receipt in YYYY-MM-DD format if available",
                "items": [
                    {"name": "item name", "price": 0.00, "quantity": 1}
                ],
                "currency": "Currency code like USD, EUR, etc",
                "tax_amount": 0.00,
                "payment_method": "Credit card, cash, etc if visible",
                "summary": "Brief summary of the receipt"
            }

            Rules:
            1. Return ONLY valid JSON, no other text
            2. If information is not available, use null or empty string
            3. Convert all amounts to numbers (not strings)
            4. Date should be in YYYY-MM-DD format if possible
            5. Total amount is the final amount paid
            6. Store name should be the business name, not address
            7. Include a brief summary of what was purchased"""
            
            # Call OpenAI API using asyncio.to_thread
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
                max_tokens=1000
            )
            
            # Extract and parse JSON response
            content = response.choices[0].message.content
            logger.info(f"OpenAI Response: {content[:200]}...")
            
            # Extract JSON from response (in case there's additional text)
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                json_str = json_match.group()
                try:
                    receipt_data = json.loads(json_str)
                    logger.info(f"✅ Successfully parsed receipt data")
                    return receipt_data
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse JSON: {e}")
                    return {"error": f"JSON parse error: {e}", "raw_response": content}
            else:
                logger.error("No JSON found in response")
                return {"error": "No JSON in response", "raw_response": content}
                
        except Exception as e:
            logger.error(f"OpenAI Vision error: {e}")
            traceback.print_exc()
            return {"error": str(e)}
    
    def format_receipt_for_display(self, receipt_data: Dict) -> str:
        """Format receipt data for user display"""
        if "error" in receipt_data:
            return f"❌ Error analyzing receipt: {receipt_data['error']}"
        
        response = "📋 **Receipt Analysis Results:**\n\n"
        
        if receipt_data.get('store_name'):
            response += f"🏪 **Store:** {receipt_data['store_name']}\n"
        
        if receipt_data.get('total_amount'):
            currency = receipt_data.get('currency', 'USD')
            response += f"💰 **Total:** {currency} {receipt_data['total_amount']:.2f}\n"
        
        if receipt_data.get('date'):
            response += f"📅 **Date:** {receipt_data['date']}\n"
        
        if receipt_data.get('tax_amount'):
            response += f"🧾 **Tax:** {receipt_data.get('tax_amount', 0):.2f}\n"
        
        if receipt_data.get('payment_method'):
            response += f"💳 **Payment:** {receipt_data['payment_method']}\n"
        
        # Show items
        items = receipt_data.get('items', [])
        if items:
            response += "\n🛒 **Items:**\n"
            for i, item in enumerate(items[:5], 1):  # Show first 5 items
                name = item.get('name', 'Unknown')
                price = item.get('price', 0)
                quantity = item.get('quantity', 1)
                response += f"  {i}. {name}"
                if quantity > 1:
                    response += f" (x{quantity})"
                response += f" - ${price:.2f}\n"
            if len(items) > 5:
                response += f"  ... and {len(items) - 5} more items\n"
        
        if receipt_data.get('summary'):
            response += f"\n📝 **Summary:** {receipt_data['summary']}\n"
        
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
            if sheet_url.startswith('http'):
                self.sheet = self.client.open_by_url(sheet_url).sheet1
            else:
                # If it's just a sheet ID
                self.sheet = self.client.open_by_key(sheet_url).sheet1
            
            logger.info(f"✅ Sheet opened: {self.sheet.title}")
            
        except Exception as e:
            logger.error(f"Failed to open sheet: {e}")
            traceback.print_exc()
            raise
        
        # Initialize headers if needed
        try:
            if not self.sheet.get_all_values():
                headers = [
                    'Timestamp', 'User ID', 'Name', 'Amount', 
                    'Date', 'Category', 'Description', 'Store',
                    'Items Summary', 'AI Analysis', 'Image Available'
                ]
                self.sheet.append_row(headers)
                logger.info("📝 Initialized sheet headers")
        except Exception as e:
            logger.error(f"Failed to init headers: {e}")
    
    def add_transaction(self, data: Dict):
        """Add a new transaction to the sheet"""
        # Format items summary
        items_summary = ""
        if data.get('items'):
            items = data['items']
            if isinstance(items, list):
                item_names = [item.get('name', '') for item in items[:3]]  # First 3 items
                items_summary = ", ".join(filter(None, item_names))
                if len(items) > 3:
                    items_summary += f" and {len(items)-3} more"
        
        row = [
            datetime.now().isoformat(),
            data.get('user_id', ''),
            data.get('name', ''),
            data.get('amount', 0),
            data.get('date', ''),
            data.get('category', ''),
            data.get('description', ''),
            data.get('store', ''),
            items_summary,
            data.get('ai_analysis', 'No'),
            'Yes' if data.get('has_image') else 'No'
        ]
        self.sheet.append_row(row)
        logger.info(f"Added: {data.get('name')} - ${data.get('amount')}")
        return True
    
    def get_transactions_by_name(self, name: str) -> List[Dict]:
        """Get all transactions for a specific person"""
        try:
            all_data = self.sheet.get_all_records()
            transactions = []
            
            for row in all_data:
                if str(row.get('Name', '')).lower() == name.lower():
                    transactions.append(row)
            
            logger.info(f"Found {len(transactions)} transactions for {name}")
            return transactions
        except Exception as e:
            logger.error(f"Error getting transactions: {e}")
            return []
    
    def get_all_names(self) -> List[str]:
        """Get list of all unique names"""
        try:
            all_data = self.sheet.get_all_records()
            names = set()
            for row in all_data:
                name = str(row.get('Name', '')).strip()
                if name:
                    names.add(name)
            return list(names)
        except Exception as e:
            logger.error(f"Error getting names: {e}")
            return []

class ReceiptBot:
    def __init__(self, sheet_manager: GoogleSheetManager):
        self.sheet = sheet_manager
        self.ai_vision = AIVisionProcessor(os.getenv('OPENAI_API_KEY'))
        
    async def start(self, update: Update, context: CallbackContext):
        """Send welcome message"""
        user = update.effective_user
        welcome_text = f"""
👋 Hello {user.first_name}!

Welcome to AI Receipt Scanner Bot! 🤖📸

I use AI to analyze receipt photos and save them to Google Sheets.

**How to use:**
1. Send me a photo of any receipt
2. I'll analyze it with AI
3. Review the analysis
4. Confirm details
5. Select category
6. ✅ Saved to Google Sheets!

**Commands:**
/add - Add transaction manually
/search <name> - Find transactions
/list - List all people
/help - Show help

Try sending me a receipt photo now! 📸
        """
        await update.message.reply_text(welcome_text, parse_mode='Markdown')
        return ConversationHandler.END
    
    async def handle_photo(self, update: Update, context: CallbackContext):
        """Handle receipt photo upload with AI analysis"""
        try:
            # Download the photo
            photo_file = await update.message.photo[-1].get_file()
            photo_bytes = await photo_file.download_as_bytearray()
            
            # Store in context
            context.user_data['receipt_photo'] = photo_bytes
            context.user_data['has_image'] = True
            
            # Start AI analysis
            await update.message.reply_text("🤖 Analyzing receipt with AI...")
            
            # Analyze with OpenAI
            receipt_data = await self.ai_vision.analyze_receipt_image(photo_bytes)
            
            # Store analysis results
            context.user_data['ai_analysis'] = receipt_data
            
            # Format and display results
            analysis_display = self.ai_vision.format_receipt_for_display(receipt_data)
            
            # Create confirmation buttons
            keyboard = [
                [InlineKeyboardButton("✅ Save to Google Sheets", callback_data="save")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            response = analysis_display + "\n\n"
            response += "Would you like to save this to Google Sheets?"
            
            await update.message.reply_text(response, reply_markup=reply_markup, parse_mode='Markdown')
            
            return CONFIRM_DETAILS
            
        except Exception as e:
            logger.error(f"Error processing photo: {e}")
            traceback.print_exc()
            await update.message.reply_text(
                "❌ Error analyzing receipt photo.\n"
                "Please try again or use /add to enter manually."
            )
            return ConversationHandler.END
    
    async def handle_confirmation(self, update: Update, context: CallbackContext):
        """Handle user confirmation to save receipt"""
        query = update.callback_query
        await query.answer()
        
        if query.data == 'cancel':
            await query.edit_message_text("❌ Receipt analysis cancelled.")
            context.user_data.clear()
            return ConversationHandler.END
        
        # User wants to save - ask for person's name
        await query.edit_message_text(
            "✅ I'll save this receipt!\n\n"
            "Please enter the person's name for this receipt:\n"
            "(Type /cancel to abort)"
        )
        return NAME
    
    async def add_receipt(self, update: Update, context: CallbackContext):
        """Start manual transaction addition"""
        context.user_data.clear()  # Clear any previous data
        await update.message.reply_text(
            "📝 Please enter the person's name for this transaction:\n"
            "(Or send a receipt photo for AI analysis)\n"
            "(Type /cancel to abort)"
        )
        return NAME
    
    async def handle_name(self, update: Update, context: CallbackContext):
        """Get person's name"""
        name = update.message.text.strip()
        if not name:
            await update.message.reply_text("Please enter a valid name:")
            return NAME
        
        context.user_data['name'] = name
        
        # Check if we have AI analysis data
        receipt_data = context.user_data.get('ai_analysis', {})
        total_amount = receipt_data.get('total_amount')
        
        if total_amount and 'error' not in receipt_data:
            currency = receipt_data.get('currency', 'USD')
            await update.message.reply_text(
                f"💰 AI detected total: {currency} {total_amount:.2f}\n"
                "Press Enter to accept, or enter a different amount:"
            )
        else:
            await update.message.reply_text(
                "💰 Enter the amount (e.g., 25.50):"
            )
        
        return AMOUNT
    
    async def handle_amount(self, update: Update, context: CallbackContext):
        """Get transaction amount"""
        user_input = update.message.text.strip()
        receipt_data = context.user_data.get('ai_analysis', {})
        detected_amount = receipt_data.get('total_amount')
        
        # If user pressed Enter and we have detected amount, use it
        if user_input == '' and detected_amount and 'error' not in receipt_data:
            amount = detected_amount
        else:
            try:
                amount = float(user_input.replace('$', '').replace(',', '').strip())
            except ValueError:
                await update.message.reply_text("❌ Invalid amount. Please enter a number (e.g., 25.50):")
                return AMOUNT
        
        context.user_data['amount'] = amount
        
        # Check for date from AI analysis
        detected_date = receipt_data.get('date')
        
        if detected_date and 'error' not in receipt_data:
            await update.message.reply_text(
                f"📅 AI detected date: {detected_date}\n"
                "Press Enter to accept, or enter a different date (YYYY-MM-DD or 'today'):"
            )
        else:
            await update.message.reply_text(
                "📅 Enter the date (YYYY-MM-DD or type 'today'):"
            )
        
        return DATE
    
    async def handle_date(self, update: Update, context: CallbackContext):
        """Get transaction date"""
        user_input = update.message.text.strip()
        receipt_data = context.user_data.get('ai_analysis', {})
        detected_date = receipt_data.get('date')
        
        # If user pressed Enter and we have detected date, use it
        if user_input == '' and detected_date and 'error' not in receipt_data:
            date_text = detected_date
        elif user_input.lower() == 'today':
            date_text = datetime.now().strftime('%Y-%m-%d')
        else:
            date_text = user_input
        
        # Validate date format (optional step)
        try:
            if date_text:  # Allow empty for now
                datetime.strptime(date_text, '%Y-%m-%d')
        except ValueError:
            await update.message.reply_text("❌ Invalid date format. Please use YYYY-MM-DD:")
            return DATE
        
        context.user_data['date'] = date_text
        
        # Store store name from AI analysis if available
        store = receipt_data.get('store_name', '')
        if store and 'error' not in receipt_data:
            context.user_data['store'] = store
        
        # Store items from AI analysis
        items = receipt_data.get('items', [])
        if items and 'error' not in receipt_data:
            context.user_data['items'] = items
        
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
        
        # Prepare summary of what will be saved
        summary = "📋 **Final Review:**\n\n"
        summary += f"👤 Name: {context.user_data.get('name')}\n"
        summary += f"💰 Amount: ${context.user_data.get('amount', 0):.2f}\n"
        summary += f"📅 Date: {context.user_data.get('date')}\n"
        
        if context.user_data.get('store'):
            summary += f"🏪 Store: {context.user_data.get('store')}\n"
        
        summary += f"📊 Category: {category}\n"
        
        items = context.user_data.get('items', [])
        if items:
            summary += f"🛒 Items: {len(items)} items detected\n"
        
        summary += "\nEnter description (optional, or type 'skip'):"
        
        await query.edit_message_text(summary, parse_mode='Markdown')
        return DESCRIPTION
    
    async def handle_description(self, update: Update, context: CallbackContext):
        """Handle description input"""
        description = update.message.text
        if description.lower() != 'skip':
            context.user_data['description'] = description
        else:
            context.user_data['description'] = ''
        
        # Save to Google Sheets
        await update.message.reply_text("💾 Saving to Google Sheets...")
        
        try:
            receipt_data = context.user_data.get('ai_analysis', {})
            transaction_data = {
                'user_id': update.effective_user.id,
                'name': context.user_data.get('name'),
                'amount': context.user_data.get('amount'),
                'date': context.user_data.get('date'),
                'category': context.user_data.get('category'),
                'description': context.user_data.get('description', ''),
                'store': context.user_data.get('store', ''),
                'items': context.user_data.get('items', []),
                'ai_analysis': 'Yes' if 'ai_analysis' in context.user_data else 'No',
                'has_image': context.user_data.get('has_image', False)
            }
            
            # Save to Google Sheets
            self.sheet.add_transaction(transaction_data)
            
            # Success message
            success_msg = f"✅ **Receipt saved successfully!**\n\n"
            success_msg += f"👤 **Name:** {transaction_data['name']}\n"
            success_msg += f"💰 **Amount:** ${transaction_data['amount']:.2f}\n"
            success_msg += f"📅 **Date:** {transaction_data['date']}\n"
            success_msg += f"📊 **Category:** {transaction_data['category']}\n"
            
            if transaction_data.get('store'):
                success_msg += f"🏪 **Store:** {transaction_data['store']}\n"
            
            if transaction_data.get('description'):
                success_msg += f"📝 **Description:** {transaction_data['description']}\n"
            
            if transaction_data['has_image']:
                success_msg += "📸 **Receipt image:** Processed with AI\n"
            
            items = transaction_data.get('items', [])
            if items:
                success_msg += f"🛒 **Items:** {len(items)} items recorded\n"
            
            success_msg += "\nUse /search to view transactions or send another receipt!"
            
            await update.message.reply_text(success_msg, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Error saving: {e}")
            traceback.print_exc()
            await update.message.reply_text("❌ Error saving receipt to Google Sheets.")
        
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
            
            response = f"📊 **Transactions for {name}:**\n\n"
            total = 0
            
            for i, transaction in enumerate(transactions, 1):
                try:
                    amount = float(transaction.get('Amount', 0))
                except (ValueError, TypeError):
                    amount = 0
                total += amount
                
                response += (
                    f"**{i}. Date:** {transaction.get('Date', 'N/A')}\n"
                    f"**Amount:** ${amount:.2f}\n"
                    f"**Category:** {transaction.get('Category', 'N/A')}\n"
                )
                
                store = transaction.get('Store', '')
                if store:
                    response += f"**Store:** {store}\n"
                
                items = transaction.get('Items Summary', '')
                if items:
                    response += f"**Items:** {items}\n"
                
                desc = transaction.get('Description', '')
                if desc:
                    response += f"**Note:** {desc}\n"
                
                if transaction.get('AI Analysis') == 'Yes':
                    response += f"**🤖 AI analyzed**\n"
                
                if transaction.get('Image Available') == 'Yes':
                    response += f"**📸 Has receipt image**\n"
                
                response += f"{'─' * 30}\n"
            
            response += f"\n💰 **Total:** ${total:.2f}"
            response += f"\n📊 **Count:** {len(transactions)} transactions"
            
            if len(response) > 4000:
                chunks = [response[i:i+4000] for i in range(0, len(response), 4000)]
                for chunk in chunks:
                    await update.message.reply_text(chunk, parse_mode='Markdown')
            else:
                await update.message.reply_text(response, parse_mode='Markdown')
                
        except Exception as e:
            logger.error(f"Error fetching: {e}")
            traceback.print_exc()
            await update.message.reply_text("❌ Error fetching transactions.")
    
    async def list_names(self, update: Update, context: CallbackContext):
        """List all names in the database"""
        try:
            names = self.sheet.get_all_names()
            if names:
                response = "📋 **People in records:**\n\n"
                for i, name in enumerate(sorted(names), 1):
                    response += f"{i}. {name}\n"
                response += "\nUse `/search <name>` to see transactions"
            else:
                response = "No records found yet."
            
            await update.message.reply_text(response, parse_mode='Markdown')
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
🤖 **AI Receipt Scanner Bot Help**

**Main Features:**
📸 Send any receipt photo - AI analyzes it automatically!
🤖 GPT-4 Vision extracts details with high accuracy
📊 Saves to Google Sheets with rich data

**Commands:**
/start - Welcome message
/add - Add transaction manually
/search <name> - Find transactions by name
/list - List all people
/help - This message

**How it works:**
1. Send a receipt photo
2. AI analyzes and extracts details
3. Review AI findings
4. Confirm to save
5. Add person's name
6. Select category
7. Add description
8. ✅ Saved to Google Sheets!

**Example:**
/search John Doe
Shows all John's receipts
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')

def main():
    """Start the bot"""
    print("🚀 Starting AI Receipt Scanner Bot with GPT-4 Vision...")
    
    # Get tokens
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN missing")
        return
    
    if not OPENAI_API_KEY:
        print("⚠️  OPENAI_API_KEY missing - AI features disabled")
    else:
        print("✅ OpenAI API key found")
    
    print("📊 Initializing Google Sheets...")
    try:
        sheet_manager = GoogleSheetManager()
        print("✅ Google Sheets ready")
    except Exception as e:
        print(f"❌ Google Sheets failed: {e}")
        traceback.print_exc()
        return
    
    # Create bot
    bot = ReceiptBot(sheet_manager)
    
    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Conversation handler for photo analysis (FIXED: removed per_message=True)
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
            ],
            DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_description),
                CommandHandler('cancel', bot.cancel)
            ]
        },
        fallbacks=[CommandHandler('cancel', bot.cancel)],
        allow_reentry=True
    )
    
    # Conversation handler for manual addition (FIXED: removed per_message=True)
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
            ],
            DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_description),
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
    application.add_handler(CommandHandler('cancel', bot.cancel))
    
    # Error handler
    async def error_handler(update: object, context: CallbackContext) -> None:
        """Log errors."""
        logger.error(f"Exception while handling update: {update}", exc_info=context.error)
        if context.error:
            await context.bot.send_message(
                chat_id=update.effective_chat.id if update and update.effective_chat else None,
                text=f"An error occurred: {context.error}"
            )
    
    application.add_error_handler(error_handler)
    
    # Start bot with webhook for Render
    print("🤖 Bot is running...")
    print("📱 Send /start to your bot on Telegram")
    print("📸 Try sending a receipt photo for AI analysis!")
    
    # Check if running on Render
    IS_RENDER = os.getenv('RENDER', '').lower() == 'true'
    
    if IS_RENDER:
        print("🌐 Running on Render with webhook...")
        PORT = int(os.getenv('PORT', 10000))
        WEBHOOK_URL = os.getenv('WEBHOOK_URL')
        
        if WEBHOOK_URL:
            print(f"🌐 Webhook URL: {WEBHOOK_URL}")
            # Set webhook
            application.run_webhook(
                listen="0.0.0.0",
                port=PORT,
                url_path=TELEGRAM_TOKEN,
                webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}",
                drop_pending_updates=True
            )
        else:
            print("⚠️  WEBHOOK_URL not set, using polling")
            application.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES
            )
    else:
        # Local development
        print("🏠 Running locally with polling...")
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )

if __name__ == '__main__':
    main()
