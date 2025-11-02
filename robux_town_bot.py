# robux_town_bot.py
# Robux Town — Automatic Order threads (Python / discord.py 2.x)
# - /post_autoorder posts the main embed with your banner + logo and a button
# - Clicking "Automatic Order" opens a THREAD ticket and runs a 5-step wizard:
#   (1/5) Start?  (2/5) Amount  (3/5) Confirm  (4/5) Payment method  (5/5) Method details
# - Crypto: shows coin picker + address from env
# - PayPal (Eneba): sends your Eneba link
# - Card (G2A): sends your G2A link
# - Giftcards: sends your instructions
# - Minimal SQLite logging of orders

import os
import sqlite3
from typing import Optional, Literal

import discord
from discord import app_commands
from discord.ext import commands

# =========================
# 1) CONFIG (ENV VARS)
# =========================
TOKEN                 = os.getenv("DISCORD_TOKEN", "PUT_YOUR_TOKEN_HERE")  # <- set in env
GUILD_ID              = int(os.getenv("GUILD_ID", "0"))                    # optional for testing
ORDER_POST_CHANNEL_ID = int(os.getenv("ORDER_POST_CHANNEL_ID", "0"))       # where /post_autoorder will send the panel
TICKETS_CHANNEL_ID    = int(os.getenv("TICKETS_CHANNEL_ID", "0"))          # channel to create threads in

BOT_EMOJI_ID          = int(os.getenv("BOT_EMOJI_ID", "0"))                # custom emoji for the button (optional)

# Commerce settings
MIN_ROBUX             = int(os.getenv("MIN_ROBUX", "10000"))
USD_PER_1K            = float(os.getenv("USD_PER_1K", "1.0"))              # $1.0 per 1,000 Robux (example)

# Payment links / addresses (edit anytime without redeploy by changing env + restart)
ENEBA_LINK            = os.getenv("ENEBA_LINK", "https://eneba.com/your-paypal-product")
G2A_LINK              = os.getenv("G2A_LINK", "https://www.g2a.com/your-card-product")

# Giftcard instructions
GIFTCARD_INSTRUCTIONS = os.getenv(
    "GIFTCARD_INSTRUCTIONS",
    "Send a clear photo or digital code (Rewarble, Steam, etc.). Staff will verify value."
)

# Crypto addresses (editable)
CRYPTO_BTC = os.getenv("CRYPTO_BTC", "bc1q_your_bitcoin_address")
CRYPTO_LTC = os.getenv("CRYPTO_LTC", "ltc1q_your_litecoin_address")
CRYPTO_ETH = os.getenv("CRYPTO_ETH", "0xYourEthereumAddress")
CRYPTO_SOL = os.getenv("CRYPTO_SOL", "YourSolanaAddress")
CRYPTO_USDT= os.getenv("CRYPTO_USDT","TRC20_or_ERC20_address")

# Media
BANNER_URL = os.getenv("BANNER_URL", "https://i.ibb.co/ZRzkHH9N/robux-town-automatic-order.png")
LOGO_URL   = os.getenv("LOGO_URL",   "https://i.ibb.co/FkDYg7gc/robux-town.png")

DB_PATH    = os.getenv("DB_PATH", "orders.db")

INTENTS = discord.Intents.default()
INTENTS.message_content = False  # we don't need raw message content for this flow

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# =========================
# 2) DATA LAYER (SQLite)
# =========================
def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        amount INTEGER NOT NULL,
        price_usd REAL NOT NULL,
        payment_method TEXT NOT NULL,
        coin TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        thread_id INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    con.commit()
    con.close()

def db_insert_order(user_id: int, username: str, amount: int, price_usd: float,
                    payment_method: str, coin: Optional[str], thread_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO orders (user_id, username, amount, price_usd, payment_method, coin, thread_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, username, amount, price_usd, payment_method, coin, thread_id))
    con.commit()
    con.close()

# =========================
# 3) UI COMPONENTS
# =========================
class StartOrderView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Automatic Order", style=discord.ButtonStyle.green, custom_id="autoorder:start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Create a thread ticket in the tickets channel
        tickets_ch = interaction.guild.get_channel(TICKETS_CHANNEL_ID)
        if not tickets_ch:
            return await interaction.response.send_message(
                "Ticket channel not configured. Please set TICKETS_CHANNEL_ID.", ephemeral=True
            )

        thread = await tickets_ch.create_thread(
            name=f"order-{interaction.user.name}".replace(" ", "-")[:90],
            type=discord.ChannelType.public_thread
        )
        await interaction.response.send_message(
            f"Opened your order thread: {thread.mention}", ephemeral=True
        )

        # Step (1/5) — Start?
        view = ConfirmStartView()
        embed = discord.Embed(
            title="Would you like to start buying Robux? (1/5)",
            description='Please click **"Yes"** if you would like to start purchasing your Robux.',
            color=discord.Color.blurple()
        )
        embed.set_thumbnail(url=LOGO_URL)
        await thread.send(content=interaction.user.mention, embed=embed, view=view)


class ConfirmStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green, custom_id="order:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Step (2/5) — Amount
        await interaction.response.send_modal(AmountModal(title="How much Robux? (2/5)"))

    @discord.ui.button(label="No", style=discord.ButtonStyle.gray, custom_id="order:no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Okay, closing this order thread.", ephemeral=True)
        try:
            await interaction.channel.edit(archived=True, locked=True)
        except Exception:
            pass


class AmountModal(discord.ui.Modal, title="Robux Amount (2/5)"):
    amount = discord.ui.TextInput(
        label="Enter amount of Robux (min: %d)" % MIN_ROBUX,
        placeholder="e.g., 10000",
        required=True,
        max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Validate amount
        try:
            amt = int(str(self.amount.value).replace(",", "").strip())
        except ValueError:
            return await interaction.response.send_message(
                "Please enter a valid integer amount.", ephemeral=True
            )

        if amt < MIN_ROBUX:
            return await interaction.response.send_message(
                f"Minimum order amount is **{MIN_ROBUX:,}** Robux.", ephemeral=True
            )

        price = (amt / 1000.0) * USD_PER_1K

        # Step (3/5) — Confirm amount & price
        desc = (
            f"Are you sure you want to purchase **{amt:,}** Robux?\n\n"
            f"**Current Rate:** ${USD_PER_1K:.2f} per 1,000 Robux\n"
            f"**Price in USD:** ${price:.2f}"
        )
        embed = discord.Embed(
            title="Confirm your purchase (3/5)",
            description=desc,
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=LOGO_URL)

        view = ConfirmAmountView(amount=amt, price=price)
        await interaction.response.send_message(embed=embed, view=view)


class ConfirmAmountView(discord.ui.View):
    def __init__(self, amount: int, price: float):
        super().__init__(timeout=180)
        self.amount = amount
        self.price = price

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green, custom_id="order:confirm_amount")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Step (4/5) — Payment method
        embed = discord.Embed(
            title="Please select your preferred payment method (4/5)",
            description="Choose one option below.",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=LOGO_URL)
        view = PaymentMethodView(amount=self.amount, price=self.price)
        await interaction.response.send_message(embed=embed, view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.gray, custom_id="order:cancel_amount")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Purchase canceled. You can start again anytime.", ephemeral=True)


class PaymentMethodView(discord.ui.View):
    def __init__(self, amount: int, price: float):
        super().__init__(timeout=240)
        self.amount = amount
        self.price = price

    @discord.ui.select(
        placeholder="Select your payment method",
        min_values=1, max_values=1,
        options=[
            discord.SelectOption(label="Cryptocurrency", description="BTC, LTC, ETH, SOL, USDT"),
            discord.SelectOption(label="PayPal (Powered by Eneba)"),
            discord.SelectOption(label="Card (Powered by G2A)"),
            discord.SelectOption(label="Giftcards")
        ]
    )
    async def method_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        method = select.values[0]
        if method == "Cryptocurrency":
            # Step (5/5) — choose coin, then show address
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Choose your coin (5/5)",
                    description="Pick a cryptocurrency below.",
                    color=discord.Color.dark_teal()
                ),
                view=CryptoChoiceView(amount=self.amount, price=self.price),
                ephemeral=False
            )
        elif method == "PayPal (Powered by Eneba)":
            embed = discord.Embed(
                title="Pay with PayPal (Eneba)",
                description=(
                    f"Please purchase via Eneba:\n{ENEBA_LINK}\n\n"
                    "After completing payment, reply here with your **order ID / proof**, and staff will verify.\n"
                    f"**Order Summary:** {self.amount:,} Robux — **${self.price:.2f}**"
                ),
                color=discord.Color.green()
            )
            db_insert_order(interaction.user.id, interaction.user.name, self.amount, self.price, "PayPal (Eneba)", None, interaction.channel.id)
            await interaction.response.send_message(embed=embed)
        elif method == "Card (Powered by G2A)":
            embed = discord.Embed(
                title="Pay with Card (G2A)",
                description=(
                    f"Please purchase via G2A:\n{G2A_LINK}\n\n"
                    "After completing payment, reply here with your **order ID / proof**, and staff will verify.\n"
                    f"**Order Summary:** {self.amount:,} Robux — **${self.price:.2f}**"
                ),
                color=discord.Color.orange()
            )
            db_insert_order(interaction.user.id, interaction.user.name, self.amount, self.price, "Card (G2A)", None, interaction.channel.id)
            await interaction.response.send_message(embed=embed)
        else:
            # Giftcards
            embed = discord.Embed(
                title="Pay with Giftcards",
                description=(
                    GIFTCARD_INSTRUCTIONS
                    + f"\n\n**Order Summary:** {self.amount:,} Robux — **${self.price:.2f}**"
                ),
                color=discord.Color.purple()
            )
            db_insert_order(interaction.user.id, interaction.user.name, self.amount, self.price, "Giftcards", None, interaction.channel.id)
            await interaction.response.send_message(embed=embed)


class CryptoChoiceView(discord.ui.View):
    def __init__(self, amount: int, price: float):
        super().__init__(timeout=240)
        self.amount = amount
        self.price = price

    @discord.ui.select(
        placeholder="Select coin",
        min_values=1, max_values=1,
        options=[
            discord.SelectOption(label="Bitcoin", description="BTC"),
            discord.SelectOption(label="Litecoin", description="LTC"),
            discord.SelectOption(label="Ethereum", description="ETH"),
            discord.SelectOption(label="Solana", description="SOL"),
            discord.SelectOption(label="Tether (USDT)", description="USDT"),
        ]
    )
    async def coin_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        coin = select.values[0]
        address_map = {
            "Bitcoin": CRYPTO_BTC,
            "Litecoin": CRYPTO_LTC,
            "Ethereum": CRYPTO_ETH,
            "Solana": CRYPTO_SOL,
            "Tether (USDT)": CRYPTO_USDT,
        }
        addr = address_map.get(coin, "N/A")

        desc = (
            f"**Send ${self.price:.2f} USD** worth of **{coin}** to the address below.\n"
            f"Address: `{addr}`\n\n"
            "After sending, reply here with your **TXID / proof**. Staff will verify and deliver your Robux.\n"
            f"**Order Summary:** {self.amount:,} Robux — **${self.price:.2f}**"
        )
        embed = discord.Embed(
            title=f"Pay with {coin}",
            description=desc,
            color=discord.Color.dark_teal()
        )
        db_insert_order(interaction.user.id, interaction.user.name, self.amount, self.price, "Crypto", coin, interaction.channel.id)
        await interaction.response.send_message(embed=embed)


# =========================
# 4) SLASH COMMANDS
# =========================
@bot.event
async def on_ready():
    db_init()
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception as e:
        print("Sync error:", e)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

def _button_emoji() -> Optional[discord.PartialEmoji]:
    if BOT_EMOJI_ID and BOT_EMOJI_ID != 0:
        return bot.get_emoji(BOT_EMOJI_ID)
    return None

@app_commands.checks.has_permissions(manage_guild=True)
@bot.tree.command(name="post_autoorder", description="Post the 'Automatic Order' embed with button.")
@app_commands.describe(channel="Channel to post in (defaults to ORDER_POST_CHANNEL_ID)")
async def post_autoorder(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not channel:
        channel = interaction.guild.get_channel(ORDER_POST_CHANNEL_ID)
    if not channel:
        return await interaction.response.send_message("Channel not set. Provide a channel or set ORDER_POST_CHANNEL_ID.", ephemeral=True)

    # Main embed
    embed = discord.Embed(
        title="Robux Town — Automatic Order",
        description=(
            "Click the button below to start your **automatic order**.\n"
            f"Minimum order: **{MIN_ROBUX:,} Robux**\n"
            f"Current rate: **${USD_PER_1K:.2f} per 1,000 Robux**"
        ),
        color=discord.Color.dark_green()
    )
    embed.set_thumbnail(url=LOGO_URL)
    embed.set_image(url=BANNER_URL)

    view = StartOrderView()
    # optional emoji
    emoji = _button_emoji()
    btn = view.children[0]  # the single button
    if emoji:
        btn.emoji = emoji

    await channel.send(embed=embed, view=view)
    await interaction.response.send_message(f"Posted the automatic order panel in {channel.mention}.", ephemeral=True)

# Simple staff command to mark an order thread as done (optional)
@app_commands.checks.has_permissions(manage_messages=True)
@bot.tree.command(name="order_done", description="Staff: close this order thread.")
async def order_done(interaction: discord.Interaction):
    if isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("Closing thread. Thank you!", ephemeral=True)
        try:
            await interaction.channel.edit(archived=True, locked=True)
        except Exception:
            pass
    else:
        await interaction.response.send_message("Run this inside an order thread.", ephemeral=True)

# =========================
# 5) RUN
# =========================
bot.run(TOKEN)
