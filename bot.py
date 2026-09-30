"""Single Discord voice bot with slash commands and a CLI prompt.

Usage:
    python bot.py                                  # slash commands + interactive prompt
    python bot.py --guild 123 --channel 456        # also join + play on start
Token: DISCORD_TOKEN env var, or "token" in config.json.
Slash commands are limited to "owner_ids" in config.json (default: the bot's owner).
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
from typing import Optional, Union

import discord
from discord import app_commands

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

VoiceLike = Union[discord.VoiceChannel, discord.StageChannel]

HELP = """CLI commands:
  join <server_id> <channel_id>   join a voice channel (and start playing)
  play | stop | leave             control audio / connection
  volume <0-200>                  volume in percent
  loop on|off                     repeat audio when it ends
  status | help | quit
Slash commands (in Discord): /join /play /stop /leave /volume /loop /status"""


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


class OwnerTree(app_commands.CommandTree):
    """Command tree that only lets allowed users run commands."""

    def __init__(self, client, bot):
        super().__init__(client)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.bot.is_allowed(interaction.user.id):
            return True
        await interaction.response.send_message(
            "You are not allowed to control this bot.", ephemeral=True
        )
        return False


class VoiceBot:
    def __init__(self, audio_file, volume=100, loop=True, owner_ids=()):
        self.client = discord.Client(intents=discord.Intents.default())
        self.tree = OwnerTree(self.client, self)
        self.audio_file = audio_file
        self.volume = max(0, min(200, volume)) / 100
        self.loop_audio = loop
        self.owner_ids = {int(i) for i in owner_ids}
        self._gen = 0  # bumps on every manual play/stop so stale "after" callbacks are ignored
        self.vc = None
        self.register_commands()

    # ---------- permissions ----------
    async def is_allowed(self, user_id):
        if not self.owner_ids:  # default: whoever owns the bot application
            app = await self.client.application_info()
            if app.team:
                self.owner_ids = {m.id for m in app.team.members}
            else:
                self.owner_ids = {app.owner.id}
        return user_id in self.owner_ids

    # ---------- core actions (all return a message string) ----------
    def connected(self):
        return self.vc is not None and self.vc.is_connected()

    async def join_channel(self, channel: VoiceLike) -> str:
        try:
            if self.connected() and self.vc.guild.id == channel.guild.id:
                await self.vc.move_to(channel)
            else:
                if self.connected():  # connected in another server
                    await self.vc.disconnect(force=True)
                self.vc = await channel.connect(timeout=30, reconnect=True)
        except discord.Forbidden:
            return "Missing Connect/Speak permission in that channel."
        except Exception as e:  # noqa: BLE001
            return f"Could not join: {e}"
        return f"Joined {channel.name}. " + self.play()

    async def join(self, guild_id, channel_id) -> str:
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return "Server not found. Is the bot invited to that server?"
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except discord.NotFound:
                return "Channel not found."
            except discord.Forbidden:
                return "No permission to see that channel."
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return "That ID is not a voice channel."
        return await self.join_channel(channel)

    def play(self) -> str:
        if not self.connected():
            return "Not in a voice channel. Use join first."
        if not os.path.isfile(self.audio_file):
            return f"Audio file not found: {self.audio_file}"
        self._gen += 1
        gen = self._gen
        if self.vc.is_playing() or self.vc.is_paused():
            self.vc.stop()  # its callback carries an old gen, so it is ignored
        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(self.audio_file, options="-vn"),
                volume=self.volume,
            )
        except (discord.ClientException, OSError) as e:
            return f"Cannot start ffmpeg ({e}). Install FFmpeg and add it to PATH."

        def after(error):
            if error:
                print(f"! Playback error: {error}")
            if gen == self._gen and self.loop_audio:
                asyncio.run_coroutine_threadsafe(self._replay(gen), self.client.loop)

        self.vc.play(source, after=after)
        return f"Playing {os.path.basename(self.audio_file)}."

    async def _replay(self, gen):
        if gen == self._gen and self.connected():
            self.play()

    def stop(self) -> str:
        self._gen += 1
        if self.connected() and self.vc.is_playing():
            self.vc.stop()
            return "Audio stopped."
        return "Nothing playing."

    async def leave(self) -> str:
        self._gen += 1
        was = self.connected()
        if was:
            await self.vc.disconnect(force=True)
        self.vc = None
        return "Left voice channel." if was else "Not in a voice channel."

    def set_volume(self, percent) -> str:
        self.volume = max(0, min(200, percent)) / 100
        if self.connected() and self.vc.source:
            self.vc.source.volume = self.volume
        return f"Volume: {int(self.volume * 100)}%"

    def set_loop(self, enabled) -> str:
        self.loop_audio = bool(enabled)
        return f"Loop: {'on' if self.loop_audio else 'off'}"

    def status(self) -> str:
        where = self.vc.channel.name if self.connected() else "-"
        playing = self.connected() and self.vc.is_playing()
        return (f"Channel: {where} | playing: {playing} | loop: {self.loop_audio} "
                f"| volume: {int(self.volume * 100)}% | file: {os.path.basename(self.audio_file)}")

    # ---------- slash commands ----------
    def register_commands(self):
        tree, bot = self.tree, self

        @tree.command(name="join", description="Join a voice channel and play the audio")
        @app_commands.describe(channel="Voice channel (default: the one you are in)")
        @app_commands.guild_only()
        async def join_cmd(inter: discord.Interaction, channel: Optional[VoiceLike] = None):
            if channel is None:
                voice = getattr(inter.user, "voice", None)
                if voice is None or voice.channel is None:
                    await inter.response.send_message(
                        "Join a voice channel first, or pick one in the command.", ephemeral=True)
                    return
                channel = voice.channel
            await inter.response.defer(ephemeral=True)  # connecting can take a few seconds
            await inter.followup.send(await bot.join_channel(channel), ephemeral=True)

        @tree.command(name="play", description="Start (or restart) the audio")
        async def play_cmd(inter: discord.Interaction):
            await inter.response.send_message(bot.play(), ephemeral=True)

        @tree.command(name="stop", description="Stop the audio (stay in the channel)")
        async def stop_cmd(inter: discord.Interaction):
            await inter.response.send_message(bot.stop(), ephemeral=True)

        @tree.command(name="leave", description="Leave the voice channel")
        async def leave_cmd(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True)
            await inter.followup.send(await bot.leave(), ephemeral=True)

        @tree.command(name="volume", description="Set volume (0-200 percent)")
        @app_commands.describe(percent="0 to 200")
        async def volume_cmd(inter: discord.Interaction, percent: app_commands.Range[int, 0, 200]):
            await inter.response.send_message(bot.set_volume(percent), ephemeral=True)

        @tree.command(name="loop", description="Repeat the audio when it ends")
        @app_commands.describe(enabled="True to loop, False to play once")
        async def loop_cmd(inter: discord.Interaction, enabled: bool):
            await inter.response.send_message(bot.set_loop(enabled), ephemeral=True)

        @tree.command(name="status", description="Show what the bot is doing")
        async def status_cmd(inter: discord.Interaction):
            await inter.response.send_message(bot.status(), ephemeral=True)

        @self.client.event
        async def on_guild_join(guild):
            await bot.sync_guild(guild)

    async def sync_guild(self, guild):
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            return True
        except discord.HTTPException as e:
            print(f"! Could not register slash commands in '{guild.name}': {e}\n"
                  "  Re-invite the bot with the 'applications.commands' scope.")
            return False

    async def sync_all(self):
        ok = 0
        for guild in self.client.guilds:
            ok += await self.sync_guild(guild)
        print(f"Slash commands registered in {ok}/{len(self.client.guilds)} server(s).")

    # ---------- CLI ----------
    async def handle(self, line):
        parts = line.strip().split()
        if not parts:
            return True
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "join":
                print(await self.join(int(args[0]), int(args[1])))
            elif cmd == "play":
                print(self.play())
            elif cmd == "stop":
                print(self.stop())
            elif cmd == "leave":
                print(await self.leave())
            elif cmd == "volume":
                print(self.set_volume(float(args[0])))
            elif cmd == "loop":
                print(self.set_loop(args[0].lower() in ("on", "1", "true", "yes")))
            elif cmd == "status":
                print(self.status())
            elif cmd in ("help", "?"):
                print(HELP)
            elif cmd in ("quit", "exit"):
                return False
            else:
                print("Unknown command. Type 'help'.")
        except (IndexError, ValueError):
            print("! Bad arguments. Type 'help'.")
        return True

    async def repl(self):
        loop = asyncio.get_running_loop()
        print("Type 'help' for commands.")
        while True:
            try:
                line = await loop.run_in_executor(None, input, "> ")
            except EOFError:  # no stdin (e.g. run as a service): just keep running
                await asyncio.Event().wait()
            if not await self.handle(line):
                return


async def amain(args):
    cfg = load_config()
    token = os.environ.get("DISCORD_TOKEN") or cfg.get("token", "").strip()
    if not token:
        sys.exit("No token. Set DISCORD_TOKEN or put it in config.json.")

    audio = args.audio or cfg.get("audio_file", "audio/recording.mp3")
    if not os.path.isabs(audio):
        audio = os.path.join(BASE_DIR, audio)
    volume = args.volume if args.volume is not None else cfg.get("volume", 100)
    loop = not args.no_loop and cfg.get("loop", True)

    if not shutil.which("ffmpeg"):
        print("! ffmpeg not found on PATH - audio will not play.")

    bot = VoiceBot(audio, volume, loop, cfg.get("owner_ids", []))
    runner = asyncio.create_task(bot.client.start(token))
    ready = asyncio.create_task(bot.client.wait_until_ready())
    done, _ = await asyncio.wait({runner, ready}, return_when=asyncio.FIRST_COMPLETED)
    if runner in done:  # login failed
        ready.cancel()
        try:
            runner.result()
        except discord.LoginFailure:
            sys.exit("Login failed: invalid bot token.")
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Could not start bot: {e}")
    print(f"Logged in as {bot.client.user}")

    try:
        await bot.sync_all()
        if args.guild and args.channel:
            print(await bot.join(args.guild, args.channel))
        await bot.repl()
    finally:
        await bot.leave()
        await bot.client.close()


def main():
    p = argparse.ArgumentParser(description="Single Discord voice bot (slash commands + CLI)")
    p.add_argument("--guild", type=int, help="server ID to join on start")
    p.add_argument("--channel", type=int, help="voice channel ID to join on start")
    p.add_argument("--audio", help="audio file (default from config.json)")
    p.add_argument("--volume", type=float, help="0-200 percent")
    p.add_argument("--no-loop", action="store_true", help="play once instead of looping")
    args = p.parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
