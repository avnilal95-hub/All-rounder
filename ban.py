import os
from typing import Literal, Optional, Dict
import discord
from discord import app_commands
from discord.ext import commands

# ==========================================
# 1. BOT SETUP & INTENTS
# ==========================================
intents = discord.Intents.default()
intents.members = True          # Required to check user ranks
intents.message_content = True  # Required if you add prefix commands later

bot = commands.Bot(command_prefix="!", intents=intents)

# Dictionary to remember the log channel for each server in memory
ban_log_channels: Dict[int, int] = {}


# ==========================================
# 2. STARTUP & SYNC EVENT
# ==========================================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s) successfully!")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


# ==========================================
# 3. INTERACTIVE BUTTON VIEW
# ==========================================
class BanConfirmView(discord.ui.View):
    def __init__(self, author: discord.Member, target: discord.User, reason: str):
        super().__init__(timeout=30)
        self.author = author
        self.target = target
        self.reason = reason
        self.value: Optional[bool] = None

    @discord.ui.button(label="Confirm Ban", style=discord.ButtonStyle.danger, emoji="🔨")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("You cannot interact with this confirmation menu.", ephemeral=True)
        self.value = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("You cannot interact with this confirmation menu.", ephemeral=True)
        self.value = False
        self.stop()
        await interaction.response.defer()


# ==========================================
# 4. MOD-LOG HELPER FUNCTION
# ==========================================
async def send_mod_log(guild: discord.Guild, embed: discord.Embed, override_channel: Optional[discord.TextChannel] = None):
    log_channel = override_channel

    # Check if they set a channel using /ban-log
    if not log_channel:
        channel_id = ban_log_channels.get(guild.id)
        if channel_id:
            log_channel = guild.get_channel(channel_id)

    # Fallback to looking for default channel names
    if not log_channel:
        log_channel = (
            discord.utils.get(guild.text_channels, name="ban-logs") or
            discord.utils.get(guild.text_channels, name="mod-logs") or
            discord.utils.get(guild.text_channels, name="audit-log")
        )

    if log_channel and log_channel.permissions_for(guild.me).send_messages:
        await log_channel.send(embed=embed)


# ==========================================
# 5. COMMAND: /BAN-LOG
# ==========================================
@bot.tree.command(name="ban-log", description="Set the channel where ban audit logs will be posted.")
@app_commands.describe(channel="Select the text channel for ban logs")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_ban_log(interaction: discord.Interaction, channel: discord.TextChannel):
    guild = interaction.guild

    if not channel.permissions_for(guild.me).send_messages:
        embed = discord.Embed(
            title="Ban Log Configuration",
            description=f"{interaction.user.mention} I do not have permission to send messages in {channel.mention}!",
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    ban_log_channels[guild.id] = channel.id

    embed = discord.Embed(
        title="Ban Log Configuration",
        description=f"✅ Successfully set the ban log channel to {channel.mention}!",
        color=discord.Color.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"Configured by {interaction.user.name}", icon_url=interaction.user.display_avatar.url)
    
    await interaction.response.send_message(embed=embed)

@set_ban_log.error
async def set_ban_log_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        embed = discord.Embed(
            title="Ban Log Configuration",
            description=f"{interaction.user.mention} you are lacking permission of Manage Server.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# 6. COMMAND: /BAN
# ==========================================
@bot.tree.command(name="ban", description="Advanced ban system with hierarchy checks, proof, and custom logs.")
@app_commands.describe(
    user="Select a member or enter a User ID",
    reason="Reason for the ban action",
    delete_messages="Purge message history from this user",
    proof="Upload image/file proof for audit logs",
    log_channel="Override log channel for this specific ban (optional)"
)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.bot_has_permissions(ban_members=True)
async def ban(
    interaction: discord.Interaction,
    user: discord.User,
    reason: str = "No reason provided",
    delete_messages: Literal["None", "1 Hour", "1 Day", "7 Days"] = "None",
    proof: Optional[discord.Attachment] = None,
    log_channel: Optional[discord.TextChannel] = None
):
    guild = interaction.guild
    moderator: discord.Member = interaction.user

    delete_seconds_map = {"None": 0, "1 Hour": 3600, "1 Day": 86400, "7 Days": 604800}
    delete_seconds = delete_seconds_map[delete_messages]

    # --- Hierarchy & Safety Checks ---
    if user.id == moderator.id:
        embed = discord.Embed(title="Ban notification", description=f"{moderator.mention} you cannot ban yourself!", color=discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    target_member = guild.get_member(user.id)
    if target_member:
        if target_member.top_role >= guild.me.top_role or target_member == guild.owner:
            embed = discord.Embed(title="Ban notification", description=f"{moderator.mention} I cannot ban {target_member.mention} because their rank is higher than or equal to my highest role!", color=discord.Color.red())
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        is_owner = (moderator.id == guild.owner_id)
        if not is_owner and target_member.top_role >= moderator.top_role:
            embed = discord.Embed(title="Ban notification", description=f"{moderator.mention} you don't have higher rank than {target_member.mention} so you can't ban them.", color=discord.Color.red())
            return await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Confirmation Step ---
    confirm_embed = discord.Embed(
        title="Ban notification — Action Required",
        description=f"Are you sure you want to ban {user.mention} (`{user.id}`)?",
        color=discord.Color.gold()
    )
    confirm_embed.add_field(name="Reason", value=reason, inline=False)
    if proof:
        confirm_embed.add_field(name="Proof Attached", value="Yes", inline=True)

    view = BanConfirmView(author=moderator, target=user, reason=reason)
    await interaction.response.send_message(embed=confirm_embed, view=view, ephemeral=True)
    await view.wait()

    if view.value is None:
        timeout_embed = discord.Embed(title="Ban notification", description="Ban request timed out.", color=discord.Color.greyple())
        return await interaction.edit_original_response(embed=timeout_embed, view=None)

    if not view.value:
        cancel_embed = discord.Embed(title="Ban notification", description="Ban action cancelled.", color=discord.Color.green())
        return await interaction.edit_original_response(embed=cancel_embed, view=None)

    # --- DM Notification ---
    dm_sent = True
    try:
        dm_embed = discord.Embed(
            title="Ban notification",
            description=f"{user.mention} you got banned by {moderator.mention} for {reason}",
            color=discord.Color.dark_red()
        )
        if guild.icon: dm_embed.set_thumbnail(url=guild.icon.url)
        if proof: dm_embed.set_image(url=proof.url)
        await user.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False

    # --- Execute Ban ---
    try:
        await guild.ban(user, reason=f"Banned by {moderator} ({moderator.id}) | Reason: {reason}", delete_message_seconds=delete_seconds)
    except discord.HTTPException as e:
        error_embed = discord.Embed(title="Ban notification", description=f"Failed to ban {user.mention}. API Error: `{e}`", color=discord.Color.red())
        return await interaction.edit_original_response(embed=error_embed, view=None)

    # --- Server Announcement ---
    server_embed = discord.Embed(
        title="Ban notification",
        description=f"{user.mention} got banned by {moderator.mention} for {reason}",
        color=discord.Color.red()
    )
    if not dm_sent:
        server_embed.set_footer(text="Note: DM could not be delivered.")
    await interaction.followup.send(embed=server_embed)

    # --- Audit Log ---
    log_embed = discord.Embed(title="🛡️ Audit Log — Member Banned", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow())
    log_embed.add_field(name="Target User", value=f"{user.mention}\n`ID: {user.id}`", inline=True)
    log_embed.add_field(name="Moderator", value=f"{moderator.mention}\n`ID: {moderator.id}`", inline=True)
    log_embed.add_field(name="Reason", value=reason, inline=False)
    if proof: log_embed.set_image(url=proof.url)

    await send_mod_log(guild, log_embed, override_channel=log_channel)

@ban.error
async def ban_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        embed = discord.Embed(title="Ban notification", description=f"{interaction.user.mention} you are lacking permission of Ban Members.", color=discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)
    if isinstance(error, app_commands.BotMissingPermissions):
        embed = discord.Embed(title="Ban notification", description=f"{interaction.user.mention} I am lacking permission of Ban Members.", color=discord.Color.red())
        return await interaction.response.send_message(embed=embed, ephemeral=True)


# ==========================================
# 7. RUN BOT (MUST BE AT THE VERY END)
# ==========================================
token = os.getenv("TOKEN")
if not token:
    raise ValueError("Missing TOKEN environment variable!")

bot.run(token)
