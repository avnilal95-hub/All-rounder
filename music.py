import os
import re
import math
import time
import asyncio
from enum import Enum
from typing import Optional, List, Dict, Union

import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp

# ==========================================
# 1. GLOBAL CONSTANTS & CONFIGURATION
# ==========================================

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.voice_states = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# Global dictionary holding individual player engines for each server
# Guild ID -> GuildMusicPlayer instance
players: Dict[int, "GuildMusicPlayer"] = {}

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "extractflat": False,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


# ==========================================
# 2. ENUMS & UTILITY HELPER FUNCTIONS
# ==========================================

class LoopMode(Enum):
    OFF = "Off"
    TRACK = "Current Track"
    QUEUE = "Entire Queue"


def format_seconds(seconds: float) -> str:
    """Converts raw seconds into HH:MM:SS or MM:SS format string."""
    seconds = int(max(0, seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_time_to_seconds(time_str: str) -> Optional[int]:
    """Parses user time strings like '1:30', '90', '01:15:30' into integer seconds."""
    time_str = time_str.strip()
    if time_str.isdigit():
        return int(time_str)
    
    parts = time_str.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None
    return None


def render_progress_bar(elapsed: float, total: float, length: int = 18) -> str:
    """Generates a visual ASCII playhead progress bar."""
    if total <= 0:
        return "🔘" + "━" * (length - 1)
    
    ratio = min(1.0, max(0.0, elapsed / total))
    filled_length = int(round(length * ratio))
    
    bar = "━" * max(0, filled_length - 1) + "🔘" + "━" * max(0, length - filled_length)
    return bar


# ==========================================
# 3. SONG DATA MODEL
# ==========================================

class Song:
    """Represents a single audio track with rich metadata."""
    def __init__(
        self,
        title: str,
        stream_url: str,
        webpage_url: str,
        duration: float,
        thumbnail: str,
        uploader: str,
        requester: discord.Member
    ):
        self.title = title
        self.stream_url = stream_url
        self.webpage_url = webpage_url
        self.duration = duration
        self.thumbnail = thumbnail
        self.uploader = uploader
        self.requester = requester

    @classmethod
    async def extract_from_query(cls, query: str, requester: discord.Member) -> Union["Song", List["Song"]]:
        """Runs YTDL extraction asynchronously in an executor pool."""
        loop = asyncio.get_event_loop()
        
        # Check if URL or raw search query
        is_url = re.match(r"^https?://", query) is not None
        search_target = query if is_url else f"ytsearch:{query}"

        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(search_target, download=False)
        )

        if not data:
            raise ValueError("No video results found for the provided search query.")

        # If it returned a search list, take the first entry
        if "entries" in data and data["entries"]:
            data = data["entries"][0]

        return cls(
            title=data.get("title", "Unknown Title"),
            stream_url=data.get("url"),
            webpage_url=data.get("webpage_url", query),
            duration=float(data.get("duration", 0)),
            thumbnail=data.get("thumbnail", ""),
            uploader=data.get("uploader", "Unknown Artist"),
            requester=requester
        )


# ==========================================
# 4. INTERACTIVE EMBED CONTROL PANEL VIEW
# ==========================================

class MusicControlView(discord.ui.View):
    """Interactive Discord UI Buttons bound to the current playback stream."""
    def __init__(self, player: "GuildMusicPlayer"):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(label="Pause / Resume", style=discord.ButtonStyle.primary, emoji="⏯️", custom_id="btn_pause_resume")
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message("❌ I am not connected to voice.", ephemeral=True)

        if vc.is_paused():
            self.player.resume()
            await interaction.response.send_message("▶️ Playback resumed.", ephemeral=True)
        elif vc.is_playing():
            self.player.pause()
            await interaction.response.send_message("⏸️ Playback paused.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️", custom_id="btn_skip")
    async def skip_track(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.current_song:
            return await interaction.response.send_message("❌ No track to skip.", ephemeral=True)
        
        self.player.skip()
        await interaction.response.send_message("⏭️ Skipped current track.", ephemeral=True)

    @discord.ui.button(label="Loop Mode", style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="btn_loop")
    async def cycle_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.loop_mode == LoopMode.OFF:
            self.player.loop_mode = LoopMode.TRACK
        elif self.player.loop_mode == LoopMode.TRACK:
            self.player.loop_mode = LoopMode.QUEUE
        else:
            self.player.loop_mode = LoopMode.OFF

        await interaction.response.send_message(f"🔁 Loop mode set to: **{self.player.loop_mode.value}**", ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, emoji="🔀", custom_id="btn_shuffle")
    async def shuffle_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.player.queue) < 2:
            return await interaction.response.send_message("❌ Need at least 2 tracks in queue to shuffle.", ephemeral=True)
        
        self.player.shuffle()
        await interaction.response.send_message("🔀 Queue shuffled successfully!", ephemeral=True)

    @discord.ui.button(label="Stop & Clear", style=discord.ButtonStyle.danger, emoji="🛑", custom_id="btn_stop")
    async def stop_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.stop()
        await interaction.response.send_message("🛑 Playback stopped and queue cleared.", ephemeral=True)


# ==========================================
# 5. PER-GUILD MUSIC ENGINE STATE MANAGER
# ==========================================

class GuildMusicPlayer:
    """Manages playback, volume state, queues, and voice lifecycle for a single server."""
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: List[Song] = []
        self.current_song: Optional[Song] = None
        
        self.volume: float = 0.5  # 50% Default Volume
        self.loop_mode: LoopMode = LoopMode.OFF
        
        self.start_timestamp: float = 0.0
        self.pause_timestamp: float = 0.0
        self.total_paused_duration: float = 0.0
        
        self.bound_channel: Optional[discord.TextChannel] = None
        self.now_playing_msg: Optional[discord.Message] = None
        self.inactivity_task: Optional[asyncio.Task] = None

    @property
    def elapsed_seconds(self) -> float:
        """Calculates current track position accurately accounting for pause durations."""
        if self.start_timestamp == 0.0:
            return 0.0
        
        vc = self.guild.voice_client
        if vc and vc.is_paused():
            return self.pause_timestamp - self.start_timestamp - self.total_paused_duration
        
        return time.time() - self.start_timestamp - self.total_paused_duration

    def cancel_inactivity_timer(self):
        """Cancels background auto-disconnect task if active."""
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
            self.inactivity_task = None

    def start_inactivity_timer(self):
        """Schedules 3-minute auto-disconnect when queue becomes idle."""
        self.cancel_inactivity_timer()
        self.inactivity_task = bot.loop.create_task(self._inactivity_worker())

    async def _inactivity_worker(self):
        """Waits 180 seconds and disconnects if audio is still inactive."""
        await asyncio.sleep(180)
        vc = self.guild.voice_client
        if vc and not vc.is_playing() and not vc.is_paused():
            if self.bound_channel:
                embed = discord.Embed(
                    title="💤 Disconnected due to Inactivity",
                    description="Left the voice channel after 3 minutes of idle silence.",
                    color=discord.Color.dark_grey()
                )
                await self.bound_channel.send(embed=embed)
            await vc.disconnect()

    async def play_next_track(self, seek_seconds: float = 0.0):
        """Core audio loop runner handling track transitions and Loop modes."""
        self.cancel_inactivity_timer()
        vc: discord.VoiceClient = self.guild.voice_client

        if not vc or not vc.is_connected():
            return

        # Handle Loop Modes when seeking is NOT taking place
        if seek_seconds == 0.0 and self.current_song:
            if self.loop_mode == LoopMode.TRACK:
                self.queue.insert(0, self.current_song)
            elif self.loop_mode == LoopMode.QUEUE:
                self.queue.append(self.current_song)

        # Check if queue has exhausted
        if not self.queue and seek_seconds == 0.0:
            self.current_song = None
            if self.now_playing_msg:
                try:
                    await self.now_playing_msg.delete()
                except discord.HTTPException:
                    pass
                self.now_playing_msg = None

            embed = discord.Embed(
                title="📄 Queue Finished",
                description="No more songs left in queue. Add more using `/play`!",
                color=discord.Color.gold()
            )
            if self.bound_channel:
                await self.bound_channel.send(embed=embed)

            self.start_inactivity_timer()
            return

        # Fetch song to play
        if seek_seconds == 0.0:
            self.current_song = self.queue.pop(0)

        song = self.current_song

        # Configure FFmpeg Options dynamically (including seek parameter if required)
        ffmpeg_before = FFMPEG_BEFORE_OPTIONS
        if seek_seconds > 0.0:
            ffmpeg_before += f" -ss {seek_seconds}"

        ffmpeg_options = {
            "before_options": ffmpeg_before,
            "options": "-vn"
        }

        # Create FFmpeg Audio Source wrapped in Volume Control
        raw_source = discord.FFmpegPCMAudio(song.stream_url, **ffmpeg_options)
        volume_source = discord.PCMVolumeTransformer(raw_source, volume=self.volume)

        # Update Timestamps
        self.start_timestamp = time.time() - seek_seconds
        self.total_paused_duration = 0.0

        # Callback triggered when song finishes streaming
        def _audio_after(error):
            if error:
                print(f"[{self.guild.name}] FFmpeg Playback Error: {error}")
            
            # Dispatch next song processing to main asyncio loop
            asyncio.run_coroutine_threadsafe(self.play_next_track(), bot.loop)

        vc.play(volume_source, after=_audio_after)

        # Send Now Playing UI Card
        await self.send_now_playing_embed()

    async def send_now_playing_embed(self):
        """Constructs and broadcasts the dynamic Now Playing card with buttons."""
        if not self.current_song or not self.bound_channel:
            return

        song = self.current_song
        elapsed = self.elapsed_seconds
        bar = render_progress_bar(elapsed, song.duration)

        embed = discord.Embed(
            title="🎶 Now Playing",
            description=f"**[{song.title}]({song.webpage_url})**",
            color=discord.Color.blurple()
        )
        embed.set_thumbnail(url=song.thumbnail)
        
        embed.add_field(
            name="Progress",
            value=f"`{format_seconds(elapsed)}` {bar} `{format_seconds(song.duration)}`",
            inline=False
        )
        embed.add_field(name="Artist / Channel", value=f"`{song.uploader}`", inline=True)
        embed.add_field(name="Requested By", value=song.requester.mention, inline=True)
        embed.add_field(name="Volume", value=f"`{int(self.volume * 100)}%`", inline=True)
        embed.add_field(name="Loop State", value=f"`{self.loop_mode.value}`", inline=True)
        embed.add_field(name="Queue Remaining", value=f"`{len(self.queue)} tracks`", inline=True)

        view = MusicControlView(self)

        # Clean up old now playing embed to prevent channel clutter
        if self.now_playing_msg:
            try:
                await self.now_playing_msg.delete()
            except discord.HTTPException:
                pass

        self.now_playing_msg = await self.bound_channel.send(embed=embed, view=view)

    def pause(self):
        vc = self.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            self.pause_timestamp = time.time()

    def resume(self):
        vc = self.guild.voice_client
        if vc and vc.is_paused():
            self.total_paused_duration += time.time() - self.pause_timestamp
            vc.resume()

    def skip(self):
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()  # Invokes _audio_after callback automatically

    async def stop(self):
        self.queue.clear()
        self.current_song = None
        vc = self.guild.voice_client
        if vc:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await vc.disconnect()

    def shuffle(self):
        import random
        random.shuffle(self.queue)

    def set_volume(self, percent: int):
        self.volume = max(0.0, min(1.5, percent / 100.0))
        vc = self.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.volume


def get_player(guild: discord.Guild) -> GuildMusicPlayer:
    """Retrieves or instantiates a GuildMusicPlayer for the server."""
    if guild.id not in players:
        players[guild.id] = GuildMusicPlayer(guild)
    return players[guild.id]


# ==========================================
# 6. BOT EVENTS & INITIALIZATION
# ==========================================

@bot.event
async def on_ready():
    print(f"==========================================")
    print(f"🤖 Bot Online: {bot.user} (ID: {bot.user.id})")
    print(f"🔊 discord.py Version: {discord.__version__}")
    print(f"==========================================")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Successfully synced {len(synced)} Slash Commands globally.")
    except Exception as e:
        print(f"❌ Failed to sync slash commands: {e}")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Automatically disconnects if bot is left completely alone in voice channel."""
    if member.id == bot.user.id:
        return

    guild = member.guild
    vc = guild.voice_client

    if vc and vc.is_connected() and len(vc.channel.members) == 1:
        player = get_player(guild)
        await player.stop()
        if player.bound_channel:
            embed = discord.Embed(
                title="👋 Left Voice Channel",
                description="Everyone left the voice channel, so I disconnected to save bandwidth.",
                color=discord.Color.greyple()
            )
            await player.bound_channel.send(embed=embed)


# ==========================================
# 7. ADVANCED SLASH COMMAND SUITE (13 COMMANDS)
# ==========================================

# ------------------------------------------
# COMMAND 1: /PLAY
# ------------------------------------------
@bot.tree.command(name="play", description="Search YouTube or paste a URL to play high-quality music.")
@app_commands.describe(query="Search query or YouTube link")
async def cmd_play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    member = interaction.user
    if not member.voice or not member.voice.channel:
        embed = discord.Embed(
            title="❌ Connection Error",
            description="You must be connected to a Voice Channel to use this command!",
            color=discord.Color.red()
        )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    voice_channel = member.voice.channel
    guild = interaction.guild
    player = get_player(guild)
    player.bound_channel = interaction.channel

    # Ensure Voice Client connection
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        try:
            vc = await voice_channel.connect(reconnect=True, self_deaf=True)
        except Exception as e:
            return await interaction.followup.send(f"❌ Could not connect to channel: `{e}`")
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    # Extract track metadata
    try:
        song = await Song.extract_from_query(query, requester=member)
    except Exception as e:
        embed = discord.Embed(
            title="❌ Search Failed",
            description=f"Could not retrieve track info: `{e}`",
            color=discord.Color.red()
        )
        return await interaction.followup.send(embed=embed)

    player.queue.append(song)

    if vc.is_playing() or vc.is_paused():
        embed = discord.Embed(
            title="➕ Added to Queue",
            description=f"**[{song.title}]({song.webpage_url})**",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=song.thumbnail)
        embed.add_field(name="Artist", value=f"`{song.uploader}`", inline=True)
        embed.add_field(name="Duration", value=f"`{format_seconds(song.duration)}`", inline=True)
        embed.add_field(name="Position in Queue", value=f"`#{len(player.queue)}`", inline=True)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send("🔎 **Searching & initiating playback...**")
        await player.play_next_track()


# ------------------------------------------
# COMMAND 2: /PAUSE
# ------------------------------------------
@bot.tree.command(name="pause", description="Pause current music playback.")
async def cmd_pause(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    vc = interaction.guild.voice_client

    if not vc or not vc.is_playing():
        return await interaction.response.send_message("❌ Nothing is currently playing.", ephemeral=True)

    player.pause()
    embed = discord.Embed(title="⏸️ Playback Paused", color=discord.Color.orange())
    await interaction.response.send_message(embed=embed)


# ------------------------------------------
# COMMAND 3: /RESUME
# ------------------------------------------
@bot.tree.command(name="resume", description="Resume paused music playback.")
async def cmd_resume(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    vc = interaction.guild.voice_client

    if not vc or not vc.is_paused():
        return await interaction.response.send_message("❌ Music is not paused.", ephemeral=True)

    player.resume()
    embed = discord.Embed(title="▶️ Playback Resumed", color=discord.Color.green())
    await interaction.response.send_message(embed=embed)


# ------------------------------------------
# COMMAND 4: /SKIP
# ------------------------------------------
@bot.tree.command(name="skip", description="Skip the currently playing track.")
async def cmd_skip(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    vc = interaction.guild.voice_client

    if not vc or not player.current_song:
        return await interaction.response.send_message("❌ No track currently playing to skip.", ephemeral=True)

    skipped_title = player.current_song.title
    player.skip()

    embed = discord.Embed(
        title="⏭️ Track Skipped",
        description=f"Skipped: **{skipped_title}**",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed)


# ------------------------------------------
# COMMAND 5: /STOP
# ------------------------------------------
@bot.tree.command(name="stop", description="Stop music playback, clear queue, and disconnect.")
async def cmd_stop(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    await player.stop()

    embed = discord.Embed(
        title="🛑 Playback Stopped",
        description="Cleared queue and disconnected from voice channel.",
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed)


# ------------------------------------------
# COMMAND 6: /QUEUE
# ------------------------------------------
@bot.tree.command(name="queue", description="Display all upcoming songs in the server queue.")
async def cmd_queue(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    if not player.current_song and not player.queue:
        return await interaction.response.send_message("📄 The music queue is completely empty.", ephemeral=True)

    embed = discord.Embed(
        title=f"📜 Music Queue — {interaction.guild.name}",
        color=discord.Color.gold()
    )

    if player.current_song:
        embed.add_field(
            name="🔊 Now Playing",
            value=f"**[{player.current_song.title}]({player.current_song.webpage_url})** | `{format_seconds(player.current_song.duration)}` requested by {player.current_song.requester.mention}",
            inline=False
        )

    if player.queue:
        queue_text = ""
        total_duration = sum(s.duration for s in player.queue)
        
        for idx, song in enumerate(player.queue[:10], start=1):
            queue_text += f"**{idx}.** [{song.title}]({song.webpage_url}) — `{format_seconds(song.duration)}` | {song.requester.mention}\n"

        if len(player.queue) > 10:
            queue_text += f"\n*...and {len(player.queue) - 10} more track(s)*"

        embed.add_field(name="Up Next", value=queue_text, inline=False)
        embed.set_footer(text=f"Total Songs: {len(player.queue)} | Total Duration: {format_seconds(total_duration)} | Loop: {player.loop_mode.value}")
    else:
        embed.add_field(name="Up Next", value="*No upcoming tracks queued.*", inline=False)

    await interaction.response.send_message(embed=embed)

# ------------------------------------------
# COMMAND 7: /NOWPLAYING
# ------------------------------------------
@bot.tree.command(name="nowplaying", description="Show detailed information about the active song.")
async def cmd_nowplaying(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    if not player.current_song:
        return await interaction.response.send_message("❌ No music is currently playing.", ephemeral=True)

    await interaction.response.send_message("📡 **Refreshing Now Playing Status...**", ephemeral=True)
    await player.send_now_playing_embed()


# ------------------------------------------
# COMMAND 8: /VOLUME
# ------------------------------------------
@bot.tree.command(name="volume", description="Adjust the bot audio volume (0% to 150%).")
@app_commands.describe(percent="Volume percentage integer between 0 and 150")
async def cmd_volume(interaction: discord.Interaction, percent: app_commands.Range[int, 0, 150]):
    player = get_player(interaction.guild)
    player.set_volume(percent)

    embed = discord.Embed(
        title="🔊 Volume Adjusted",
        description=f"Audio output volume set to **{percent}%**",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)


# ------------------------------------------
# COMMAND 9: /LOOP
# ------------------------------------------
@bot.tree.command(name="loop", description="Set track or queue loop mode.")
@app_commands.describe(mode="Select loop mode option")
@app_commands.choices(mode=[
    app_commands.Choice(name="Off", value="Off"),
    app_commands.Choice(name="Current Track", value="Current Track"),
    app_commands.Choice(name="Entire Queue", value="Entire Queue")
])
async def cmd_loop(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    player = get_player(interaction.guild)

    if mode.value == "Off":
        player.loop_mode = LoopMode.OFF
    elif mode.value == "Current Track":
        player.loop_mode = LoopMode.TRACK
    else:
        player.loop_mode = LoopMode.QUEUE

    embed = discord.Embed(
        title="🔁 Loop Mode Updated",
        description=f"Loop mode set to: **{player.loop_mode.value}**",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed)


# ------------------------------------------
# COMMAND 10: /SHUFFLE
# ------------------------------------------
@bot.tree.command(name="shuffle", description="Randomly shuffle all tracks inside the queue.")
async def cmd_shuffle(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    if len(player.queue) < 2:
        return await interaction.response.send_message("❌ Need at least 2 tracks in queue to shuffle.", ephemeral=True)

    player.shuffle()
    embed = discord.Embed(
        title="🔀 Queue Shuffled",
        description=f"Successfully randomized **{len(player.queue)}** queued tracks.",
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed)


# ------------------------------------------
# COMMAND 11: /SEEK
# ------------------------------------------
@bot.tree.command(name="seek", description="Seek to a specific time position in the song (e.g. 1:30 or 90).")
@app_commands.describe(timestamp="Time position string (e.g. '1:30' or '90')")
async def cmd_seek(interaction: discord.Interaction, timestamp: str):
    await interaction.response.defer()
    player = get_player(interaction.guild)
    vc = interaction.guild.voice_client

    if not vc or not player.current_song:
        return await interaction.followup.send("❌ No track active to seek within.")

    seconds = parse_time_to_seconds(timestamp)
    if seconds is None:
        return await interaction.followup.send("❌ Invalid timestamp format! Use `MM:SS` (e.g., `1:30`) or seconds (`90`).")

    if seconds >= player.current_song.duration:
        return await interaction.followup.send("❌ Cannot seek past the total track duration!")

    vc.stop()  # Stops current stream
    await player.play_next_track(seek_seconds=float(seconds))

    embed = discord.Embed(
        title="⏩ Position Seeked",
        description=f"Jumped playback head to **{format_seconds(seconds)}**",
        color=discord.Color.teal()
    )
    await interaction.followup.send(embed=embed)


# ------------------------------------------
# COMMAND 12: /REMOVE
# ------------------------------------------
@bot.tree.command(name="remove", description="Remove a specific track index from the queue.")
@app_commands.describe(index="Queue number index to remove (1, 2, 3...)")
async def cmd_remove(interaction: discord.Interaction, index: app_commands.Range[int, 1, 500]):
    player = get_player(interaction.guild)

    if not player.queue or index > len(player.queue):
        return await interaction.response.send_message("❌ Invalid queue index provided.", ephemeral=True)

    removed = player.queue.pop(index - 1)
    embed = discord.Embed(
        title="🗑️ Track Removed",
        description=f"Removed **[{removed.title}]({removed.webpage_url})** from position `#{index}`.",
        color=discord.Color.dark_red()
    )
    await interaction.response.send_message(embed=embed)


# ------------------------------------------
# COMMAND 13: /CLEAR
# ------------------------------------------
@bot.tree.command(name="clear", description="Clear all pending tracks from the queue without stopping current song.")
async def cmd_clear(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    count = len(player.queue)
    player.queue.clear()

    embed = discord.Embed(
        title="🧹 Queue Cleared",
        description=f"Cleared **{count}** pending track(s) from queue.",
        color=discord.Color.dark_gold()
    )
    await interaction.response.send_message(embed=embed)


# ==========================================
# 8. START BOT PROCESS
# ==========================================

if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        raise ValueError("CRITICAL ERROR: Missing 'TOKEN' environment variable!")
    
    bot.run(TOKEN)
