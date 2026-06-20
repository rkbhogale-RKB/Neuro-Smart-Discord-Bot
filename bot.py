import os
import asyncio
import random
import discord
from dotenv import load_dotenv

from services.llm_service import generate_response
from services.memory_service import (
    add_message,
    get_history,
)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")


@client.event
async def on_message(message):

    # Ignore bot's own messages
    if message.author == client.user:
        return

    content = message.content.lower()

    # -------- Trigger Detection --------

    # Mention by name
    is_trigger = "neuro" in content

    # Reply to Neuro
    is_reply_to_bot = False

    if message.reference:

        replied_message = None

        # Discord sometimes already provides the replied message
        if message.reference.resolved:
            replied_message = message.reference.resolved

        else:
            try:
                replied_message = await message.channel.fetch_message(
                    message.reference.message_id
                )
            except Exception:
                replied_message = None

        if replied_message and replied_message.author == client.user:
            is_reply_to_bot = True

    # Ignore unrelated messages
    if not (is_trigger or is_reply_to_bot):
        return

    # -------------------------------

    channel_id = message.channel.id

    # Save user's message
    add_message(
        channel_id,
        "user",
        message.content,
    )

    async with message.channel.typing():

        reply = generate_response(get_history(channel_id))

        # Simulate human thinking time
        await asyncio.sleep(random.uniform(2, 4))

    # Save bot reply
    add_message(
        channel_id,
        "assistant",
        reply,
    )

    await message.channel.send(reply)


client.run(TOKEN)