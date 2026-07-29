import os
import discord
from discord.ext import commands

# Enable necessary intents
intents = discord.Intents.default()
intents.message_content = True  # Required for prefix commands like !ping

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Sync slash commands with Discord
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

# 1. Modern Slash Command (/ping)
@bot.tree.command(name="ping", description="Responds with Pong and latency!")
async def ping_slash(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)  # Convert to milliseconds
    await interaction.response.send_message(f"🏓 **Pong!** `{latency}ms`")

# 2. Traditional Prefix Command (!ping)
@bot.command(name="ping")
async def ping_prefix(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 **Pong!** `{latency}ms`")

# Get token from Railway environment variables
token = os.getenv("TOKEN")
if not token:
    raise ValueError("Missing TOKEN environment variable!")

bot.run(token)
