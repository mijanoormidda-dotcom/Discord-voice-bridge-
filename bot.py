import os
import asyncio
import queue

import discord
from discord.ext import commands, voice_recv
from dotenv import load_dotenv


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_A_TOKEN = os.getenv("BOT_A_TOKEN")
BOT_B_TOKEN = os.getenv("BOT_B_TOKEN")

if not BOT_A_TOKEN or not BOT_B_TOKEN:
    raise RuntimeError(
        "BOT_A_TOKEN and BOT_B_TOKEN must be in .env"
    )


# ============================================================
# DISCORD INTENTS
# ============================================================

intents = discord.Intents.default()

intents.guilds = True
intents.voice_states = True
intents.message_content = True


# ============================================================
# BOTS
# ============================================================

bot_a = commands.Bot(
    command_prefix="!",
    intents=intents
)

bot_b = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# GLOBAL VOICE CLIENTS
# ============================================================

voice_a = None
voice_b = None

bridge_running = False


# ============================================================
# AUDIO QUEUES
# ============================================================

class AudioBridge:

    def __init__(self):

        self.queue = queue.Queue(
            maxsize=100
        )

    def put(self, audio):

        try:
            self.queue.put_nowait(audio)

        except queue.Full:

            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass

            try:
                self.queue.put_nowait(audio)
            except queue.Full:
                pass

    def get(self):

        try:
            return self.queue.get(
                timeout=0.05
            )

        except queue.Empty:
            return None


# A → B
audio_a_to_b = AudioBridge()

# B → A
audio_b_to_a = AudioBridge()


# ============================================================
# AUDIO RECEIVER
# ============================================================

class BridgeReceiver(voice_recv.AudioSink):

    def __init__(self, bridge):

        super().__init__()

        self.bridge = bridge

    def wants_opus(self):

        return False

    def write(self, user, data):

        if not bridge_running:
            return

        if data is None:
            return

        if data.pcm:

            self.bridge.put(
                data.pcm
            )

    def cleanup(self):
        pass


# ============================================================
# AUDIO SOURCE
# ============================================================

class BridgeSource(discord.AudioSource):

    def __init__(self, bridge):

        self.bridge = bridge

    def read(self):

        audio = self.bridge.get()

        # 48 kHz
        # 20 ms
        # stereo
        # 16-bit PCM
        #
        # 48000 × 0.02 × 2 × 2
        # = 3840 bytes

        if audio is None:

            return b"\x00" * 3840

        if len(audio) >= 3840:

            return audio[:3840]

        return audio + (
            b"\x00" * (3840 - len(audio))
        )

    def is_opus(self):

        return False

    def cleanup(self):
        pass


# ============================================================
# BOT A READY
# ============================================================

@bot_a.event
async def on_ready():

    print(
        f"🔵 Bot A online: {bot_a.user}"
    )


# ============================================================
# BOT B READY
# ============================================================

@bot_b.event
async def on_ready():

    print(
        f"🟢 Bot B online: {bot_b.user}"
    )


# ============================================================
# GET CURRENT USER VOICE CHANNEL
# ============================================================

async def get_user_channel(ctx):

    if not ctx.author.voice:

        return None

    return ctx.author.voice.channel


# ============================================================
# JOIN BOT A
# ============================================================

async def join_bot_a(channel):

    global voice_a

    if voice_a:

        if voice_a.channel.id == channel.id:

            return voice_a

        await voice_a.move_to(channel)

    else:

        voice_a = await channel.connect(
            cls=voice_recv.VoiceRecvClient
        )

    return voice_a


# ============================================================
# JOIN BOT B
# ============================================================

async def join_bot_b(channel):

    global voice_b

    if voice_b:

        if voice_b.channel.id == channel.id:

            return voice_b

        await voice_b.move_to(channel)

    else:

        voice_b = await channel.connect(
            cls=voice_recv.VoiceRecvClient
        )

    return voice_b


# ============================================================
# BRIDGE COMMAND GROUP
# ============================================================

@commands.group(
    name="bridge",
    invoke_without_command=True
)
async def bridge(ctx):

    await ctx.send(
        "Use `!bridge status` to check the bridge."
    )


# ============================================================
# !bridge join A/B
# ============================================================

@bridge.command(name="join")
async def bridge_join(ctx, bot_name=None):

    global voice_a
    global voice_b

    if bot_name is None:

        await ctx.send(
            "Usage: `!bridge join A` or `!bridge join B`"
        )

        return

    bot_name = bot_name.upper()

    if bot_name not in ("A", "B"):

        await ctx.send(
            "Bot must be `A` or `B`."
        )

        return

    channel = await get_user_channel(ctx)

    if channel is None:

        await ctx.send(
            "❌ You must be inside a voice channel."
        )

        return

    try:

        if bot_name == "A":

            await join_bot_a(channel)

            await ctx.send(
                f"🔵 Bot A joined **{channel.name}**."
            )

        else:

            await join_bot_b(channel)

            await ctx.send(
                f"🟢 Bot B joined **{channel.name}**."
            )

    except Exception as e:

        await ctx.send(
            f"❌ Could not connect: `{e}`"
        )


# ============================================================
# START BRIDGE
# ============================================================

@bridge.command(name="start")
async def bridge_start(ctx):

    global bridge_running

    if voice_a is None:

        await ctx.send(
            "❌ Bot A is not connected."
        )

        return

    if voice_b is None:

        await ctx.send(
            "❌ Bot B is not connected."
        )

        return

    if voice_a.is_listening():

        # Already listening
        pass

    else:

        voice_a.listen(
            BridgeReceiver(
                audio_b_to_a
            )
        )

    if voice_b.is_listening():

        pass

    else:

        voice_b.listen(
            BridgeReceiver(
                audio_a_to_b
            )
        )

    if not voice_a.is_playing():

        voice_a.play(
            BridgeSource(
                audio_b_to_a
            )
        )

    if not voice_b.is_playing():

        voice_b.play(
            BridgeSource(
                audio_a_to_b
            )
        )

    bridge_running = True

    await ctx.send(
        "🔊 **Voice bridge started!**\n"
        "Channel A ⇄ Channel B"
    )


# ============================================================
# STOP BRIDGE
# ============================================================

@bridge.command(name="stop")
async def bridge_stop(ctx):

    global bridge_running

    bridge_running = False

    if voice_a:

        voice_a.stop_listening()

        if voice_a.is_playing():
            voice_a.stop()

    if voice_b:

        voice_b.stop_listening()

        if voice_b.is_playing():
            voice_b.stop()

    await ctx.send(
        "🛑 **Voice bridge stopped.**"
    )


# ============================================================
# LEAVE BOT A
# ============================================================

@bridge.command(name="leave")
async def bridge_leave(ctx, bot_name=None):

    global voice_a
    global voice_b

    if bot_name is None:

        await ctx.send(
            "Usage: `!bridge leave A` or `!bridge leave B`"
        )

        return

    bot_name = bot_name.upper()

    if bot_name == "A":

        if voice_a:

            await voice_a.disconnect()

            voice_a = None

            await ctx.send(
                "🔵 Bot A left the voice channel."
            )

        else:

            await ctx.send(
                "Bot A is not connected."
            )

    elif bot_name == "B":

        if voice_b:

            await voice_b.disconnect()

            voice_b = None

            await ctx.send(
                "🟢 Bot B left the voice channel."
            )

        else:

            await ctx.send(
                "Bot B is not connected."
            )

    else:

        await ctx.send(
            "Bot must be `A` or `B`."
        )


# ============================================================
# STATUS
# ============================================================

@bridge.command(name="status")
async def bridge_status(ctx):

    if voice_a:

        channel_a = voice_a.channel.name

    else:

        channel_a = "Not connected"

    if voice_b:

        channel_b = voice_b.channel.name

    else:

        channel_b = "Not connected"

    state = (
        "🟢 Running"
        if bridge_running
        else "🔴 Stopped"
    )

    message = (
        "**Voice Bridge Status**\n\n"
        f"🔵 Bot A: `{channel_a}`\n"
        f"🟢 Bot B: `{channel_b}`\n"
        f"🔊 Bridge: {state}"
    )

    await ctx.send(message)


# ============================================================
# REGISTER COMMAND GROUP
# ============================================================

bot_a.add_command(bridge)
bot_b.add_command(bridge)


# ============================================================
# RUN BOTH BOTS
# ============================================================

async def main():

    print("==============================")
    print(" Discord Voice Bridge")
    print("==============================")

    await asyncio.gather(

        bot_a.start(BOT_A_TOKEN),

        bot_b.start(BOT_B_TOKEN)
    )


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        print("Bot stopped.")