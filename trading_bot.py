import asyncio
import re
import logging
from typing import Dict, Optional, List
from datetime import datetime
import json
import os
from dotenv import load_dotenv
load_dotenv()
from dataclasses import dataclass

# Required imports (install via pip)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from pybit.unified_trading import HTTP
import yaml

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

@dataclass
class TradingSignal:
    """Data class to store parsed trading signals"""
    direction: str  # LONG or SHORT
    symbol: str
    order_type: str  # MARKET or LIMIT
    entry_price: Optional[float] = None
    take_profit: float = 0
    stop_loss: float = 0
    leverage: int = 10
    risk_percentage: float = 5.0

class ConfigManager:
    """Manages bot configuration and user settings"""
    
    def __init__(self, config_file='config.yaml'):
        self.config_file = config_file
        self.config = self.load_config()
    
    def load_config(self) -> Dict:
        """Load configuration from file"""
        default_config = {
            'users': {},
            'default_leverage': 10,
            'default_risk': 5.0,
            'max_leverage': 50,
            'max_risk': 10.0
        }
        
        try:
            with open(self.config_file, 'r') as f:
                return yaml.safe_load(f) or default_config
        except FileNotFoundError:
            return default_config
    
    def save_config(self):
        """Save configuration to file"""
        with open(self.config_file, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False)
    
    def add_user(self, user_id: int, api_key: str, api_secret: str):
        """Add user API credentials"""
        self.config['users'][str(user_id)] = {
            'api_key': api_key,
            'api_secret': api_secret,
            'leverage': self.config['default_leverage'],
            'risk': self.config['default_risk'],
            'auto_trade': False
        }
        self.save_config()
    
    def get_user_config(self, user_id: int) -> Optional[Dict]:
        """Get user configuration"""
        return self.config['users'].get(str(user_id))
    
    def update_user_setting(self, user_id: int, setting: str, value):
        """Update user setting"""
        user_config = self.get_user_config(user_id)
        if user_config:
            user_config[setting] = value
            self.save_config()
            return True
        return False

class SignalParser:
    """Parses trading signals from text messages"""
    
    @staticmethod
    def parse_signal(text: str) -> Optional[TradingSignal]:
        """
        Parse trading signal from message text
        
        Expected formats:
        LONG/SHORT
        SYMBOL/USDT
        MARKET/LIMIT ORDER [price]
        Leverage: XXX
        TP: XXX
        SL: XXX
        Use X% of capital
        """
        try:
            # Clean the text
            text = text.upper().strip()
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            
            signal = TradingSignal(
                direction="",
                symbol="",
                order_type="MARKET"
            )
            
            # Parse direction (LONG/SHORT)
            direction_match = re.search(r'(LONG|SHORT)', text)
            if not direction_match:
                return None
            signal.direction = direction_match.group(1)
            
            # Parse symbol
            symbol_match = re.search(r'([A-Z0-9]+)/USDT', text)
            if not symbol_match:
                return None
            signal.symbol = symbol_match.group(1) + 'USDT'
            
            # Parse order type and entry price
            if 'LIMIT ORDER' in text:
                signal.order_type = 'LIMIT'
                limit_match = re.search(r'LIMIT ORDER\s+([\d.]+)', text)
                if limit_match:
                    signal.entry_price = float(limit_match.group(1))
            else:
                signal.order_type = 'MARKET'
            
            # Parse leverage
            leverage_match = re.search(r'LEVERAGE:\s*(\d+)X?', text)
            if leverage_match:
                signal.leverage = int(leverage_match.group(1))
            
            # Parse take profit
            tp_match = re.search(r'TP:\s*([\d.]+)', text)
            if tp_match:
                signal.take_profit = float(tp_match.group(1))
            
            # Parse stop loss
            sl_match = re.search(r'SL:\s*([\d.]+)', text)
            if sl_match:
                signal.stop_loss = float(sl_match.group(1))
            
            # Parse risk percentage
            risk_match = re.search(r'USE\s+(\d+)%\s+OF\s+CAPITAL', text)
            if risk_match:
                signal.risk_percentage = float(risk_match.group(1))
            
            return signal
            
        except Exception as e:
            logger.error(f"Error parsing signal: {e}")
            return None

class BybitTrader:
    """Handles Bybit trading operations"""
    
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.session = HTTP(
            api_key=api_key,
            api_secret=api_secret,
            testnet=testnet
        )
    
    async def get_account_balance(self) -> Dict:
        """Get account balance"""
        try:
            result = self.session.get_wallet_balance(accountType="UNIFIED")
            return result
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return {}
    
    async def place_order(self, signal: TradingSignal, user_leverage: int, user_risk: float) -> Dict:
        """Place trading order based on signal"""
        try:
            # Set leverage
            await self.set_leverage(signal.symbol, user_leverage)
            
            # Calculate position size based on risk
            balance = await self.get_account_balance()
            if not balance or 'result' not in balance:
                raise Exception("Could not fetch account balance")
            
            # Get USDT balance
            usdt_balance = 0
            for coin in balance['result']['list'][0]['coin']:
                if coin['coin'] == 'USDT':
                    usdt_balance = float(coin['walletBalance'])
                    break
            
            if usdt_balance == 0:
                raise Exception("No USDT balance found")
            
            # Calculate position size
            risk_amount = usdt_balance * (user_risk / 100)
            
            # Get current price for market orders
            if signal.order_type == 'MARKET':
                ticker = self.session.get_tickers(category="linear", symbol=signal.symbol)
                if ticker['retCode'] != 0:
                    raise Exception("Could not fetch ticker data")
                current_price = float(ticker['result']['list'][0]['lastPrice'])
                signal.entry_price = current_price
            
            # Calculate quantity
            qty = (risk_amount * user_leverage) / signal.entry_price
            
            # Round quantity to appropriate decimal places
            qty = round(qty, 6)
            
            # Determine side
            side = "Buy" if signal.direction == "LONG" else "Sell"
            
            # Place the order
            order_params = {
                "category": "linear",
                "symbol": signal.symbol,
                "side": side,
                "orderType": "Market" if signal.order_type == "MARKET" else "Limit",
                "qty": str(qty),
                "timeInForce": "GTC"
            }
            
            if signal.order_type == "LIMIT":
                order_params["price"] = str(signal.entry_price)
            
            # Add TP/SL if provided
            if signal.take_profit > 0:
                order_params["takeProfit"] = str(signal.take_profit)
            
            if signal.stop_loss > 0:
                order_params["stopLoss"] = str(signal.stop_loss)
            
            result = self.session.place_order(**order_params)
            
            return {
                'success': result['retCode'] == 0,
                'result': result,
                'order_params': order_params
            }
            
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return {'success': False, 'error': str(e)}
    
    async def set_leverage(self, symbol: str, leverage: int):
        """Set leverage for symbol"""
        try:
            result = self.session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage)
            )
            return result['retCode'] == 0
        except Exception as e:
            logger.error(f"Error setting leverage: {e}")
            return False

class TradingBot:
    """Main trading bot class"""
    
    def __init__(self, telegram_token: str):
        self.config_manager = ConfigManager()
        self.signal_parser = SignalParser()
        self.app = Application.builder().token(telegram_token).build()
        self.setup_handlers()
    
    def setup_handlers(self):
        """Setup telegram command and message handlers"""
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("setup", self.setup_command))
        self.app.add_handler(CommandHandler("settings", self.settings_command))
        self.app.add_handler(CommandHandler("balance", self.balance_command))
        self.app.add_handler(CommandHandler("toggle", self.toggle_auto_trade))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_message = """
🤖 *Trading Signal Bot*

Welcome to the automated trading signal bot!

*Commands:*
/setup - Configure your Bybit API credentials
/settings - Adjust trading settings (leverage, risk)
/balance - Check account balance
/toggle - Enable/disable auto trading

*Setup Instructions:*
1. Use /setup to add your Bybit API credentials
2. Configure your risk and leverage settings
3. Enable auto trading with /toggle
4. Forward signals from your trading group

⚠️ *Important:* Only use API keys with futures trading permissions and consider using testnet first.
        """
        await update.message.reply_text(welcome_message, parse_mode='Markdown')
    
    async def setup_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /setup command"""
        if len(context.args) != 2:
            await update.message.reply_text(
                "Usage: /setup <api_key> <api_secret>\n\n"
                "⚠️ Send this command in a private chat for security!"
            )
            return
        
        api_key, api_secret = context.args
        user_id = update.effective_user.id
        
        # Test API credentials
        try:
            trader = BybitTrader(api_key, api_secret, testnet=False)  # Test on testnet first
            balance = await trader.get_account_balance()
            
            if not balance:
                await update.message.reply_text("❌ Invalid API credentials!")
                return
            
            # Save credentials
            self.config_manager.add_user(user_id, api_key, api_secret)
            await update.message.reply_text(
                "✅ API credentials saved successfully!\n"
                "Use /settings to configure your trading parameters."
            )
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error testing API: {str(e)}")
    
    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /settings command"""
        user_id = update.effective_user.id
        user_config = self.config_manager.get_user_config(user_id)
        
        if not user_config:
            await update.message.reply_text("❌ Please setup your API credentials with /setup first!") 
            return
        
        keyboard = [
            [InlineKeyboardButton(f"Leverage: {user_config['leverage']}x", callback_data="set_leverage")],
            [InlineKeyboardButton(f"Risk: {user_config['risk']}%", callback_data="set_risk")],
            [InlineKeyboardButton(f"Auto Trade: {'ON' if user_config['auto_trade'] else 'OFF'}", callback_data="toggle_auto")]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("⚙️ *Trading Settings*", reply_markup=reply_markup, parse_mode='Markdown')
    
    async def balance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /balance command"""
        user_id = update.effective_user.id
        user_config = self.config_manager.get_user_config(user_id)
        
        if not user_config:
            await update.message.reply_text("❌ Please setup your API credentials with /setup first!")
            return
        
        try:
            trader = BybitTrader(user_config['api_key'], user_config['api_secret'])
            balance = await trader.get_account_balance()
            
            if balance and 'result' in balance:
                usdt_balance = 0
                for coin in balance['result']['list'][0]['coin']:
                    if coin['coin'] == 'USDT':
                        usdt_balance = float(coin['walletBalance'])
                        break
                
                await update.message.reply_text(f"💰 *Account Balance*\n\nUSDT: {usdt_balance:.2f}", parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Could not fetch balance!")
                
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
    
    async def toggle_auto_trade(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle auto trading on/off"""
        user_id = update.effective_user.id
        user_config = self.config_manager.get_user_config(user_id)
        
        if not user_config:
            await update.message.reply_text("❌ Please setup your API credentials with /setup first!")
            return
        
        new_status = not user_config['auto_trade']
        self.config_manager.update_user_setting(user_id, 'auto_trade', new_status)
        
        status_text = "ON" if new_status else "OFF"
        await update.message.reply_text(f"🔄 Auto trading is now *{status_text}*", parse_mode='Markdown')
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming messages and parse trading signals"""
        user_id = update.effective_user.id
        user_config = self.config_manager.get_user_config(user_id)
        
        if not user_config or not user_config['auto_trade']:
            return
        
        # Parse signal from message
        signal = self.signal_parser.parse_signal(update.message.text)
        
        if not signal:
            return  # Not a trading signal
        
        # Create confirmation message
        signal_text = f"""
📊 *Signal Detected*

Direction: {signal.direction}
Symbol: {signal.symbol}
Type: {signal.order_type}
{f'Entry: {signal.entry_price}' if signal.entry_price else ''}
TP: {signal.take_profit}
SL: {signal.stop_loss}
Leverage: {user_config['leverage']}x
Risk: {user_config['risk']}%

Execute trade?
        """
        
        keyboard = [
            [InlineKeyboardButton("✅ Execute", callback_data=f"execute_{len(context.user_data)}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        
        # Store signal in user data
        if 'signals' not in context.user_data:
            context.user_data['signals'] = {}
        context.user_data['signals'][len(context.user_data)] = signal
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(signal_text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard callbacks"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        user_config = self.config_manager.get_user_config(user_id)
        
        if query.data.startswith("execute_"):
            signal_id = int(query.data.split("_")[1])
            signal = context.user_data.get('signals', {}).get(signal_id)
            
            if not signal:
                await query.edit_message_text("❌ Signal not found!")
                return
            
            # Execute trade
            trader = BybitTrader(user_config['api_key'], user_config['api_secret'])
            result = await trader.place_order(signal, user_config['leverage'], user_config['risk'])
            
            if result['success']:
                await query.edit_message_text(
                    f"✅ *Trade Executed Successfully*\n\n"
                    f"Order ID: {result['result'].get('result', {}).get('orderId', 'N/A')}\n"
                    f"Symbol: {signal.symbol}\n"
                    f"Side: {signal.direction}\n"
                    f"Quantity: {result['order_params']['qty']}",
                    parse_mode='Markdown'
                )
            else:
                await query.edit_message_text(f"❌ Trade failed: {result.get('error', 'Unknown error')}")
        
        elif query.data == "cancel":
            await query.edit_message_text("❌ Trade cancelled")
        
        # Handle settings callbacks
        elif query.data == "set_leverage":
            keyboard = []
            for lev in [5, 10, 20, 25, 50]:
                keyboard.append([InlineKeyboardButton(f"{lev}x", callback_data=f"leverage_{lev}")])
            keyboard.append([InlineKeyboardButton("« Back", callback_data="back_settings")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Select Leverage:", reply_markup=reply_markup)
        
        elif query.data.startswith("leverage_"):
            leverage = int(query.data.split("_")[1])
            self.config_manager.update_user_setting(user_id, 'leverage', leverage)
            await query.edit_message_text(f"✅ Leverage set to {leverage}x")
        
        elif query.data == "set_risk":
            keyboard = []
            for risk in [1, 2, 5, 10]:
                keyboard.append([InlineKeyboardButton(f"{risk}%", callback_data=f"risk_{risk}")])
            keyboard.append([InlineKeyboardButton("« Back", callback_data="back_settings")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("Select Risk Percentage:", reply_markup=reply_markup)
        
        elif query.data.startswith("risk_"):
            risk = float(query.data.split("_")[1])
            self.config_manager.update_user_setting(user_id, 'risk', risk)
            await query.edit_message_text(f"✅ Risk set to {risk}%")
    
    def run(self):
        """Start the bot"""
        logger.info("Starting Trading Signal Bot...")
        self.app.run_polling()

# Main execution
if __name__ == "__main__":
    # Load environment variables
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not TELEGRAM_TOKEN:
        print("❌ Please set TELEGRAM_BOT_TOKEN environment variable")
        exit(1)
    
    # Create and run bot
    bot = TradingBot(TELEGRAM_TOKEN)
    bot.run()