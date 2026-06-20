import random
import discord

# ---------------------------------------
# Configuration
# ---------------------------------------

REACTION_CHANCE = 0.35  # 35% chance to react


# ---------------------------------------
# Mood Database
# ---------------------------------------

MOODS = {
    "celebration": {
        "keywords": [
            "yay", "finally", "done", "fixed", "success", "passed",
            "won", "completed", "awesome", "great", "amazing",
            "promotion", "lets go", "let's go", "finished"
        ],
        "emojis": ["🥳", "🎉", "🎊", "🔥", "🚀", "👏", "💯"]
    },

    "funny": {
        "keywords": [
            "lol", "lmao", "haha", "hehe", "rofl",
            "meme", "funny", "xd", "💀"
        ],
        "emojis": ["😂", "🤣", "💀", "😆"]
    },

    "thinking": {
        "keywords": [
            "why", "how", "what", "explain",
            "question", "hmm", "maybe", "idea",
            "understand", "confused"
        ],
        "emojis": ["🤔", "🧐", "💭"]
    },

    "programming": {
        "keywords": [
            "python", "java", "javascript", "docker",
            "linux", "code", "coding", "bug", "error",
            "github", "git", "api", "sql", "database",
            "rag", "langchain", "llm"
        ],
        "emojis": ["💻", "⚡", "🔥", "🚀", "🧠"]
    },

    "love": {
        "keywords": [
            "thanks", "thank you", "ty", "love",
            "appreciate", "respect"
        ],
        "emojis": ["❤️", "💙", "💜", "🥰", "😊"]
    },

    "sad": {
        "keywords": [
            "sad", "cry", "failed", "lost",
            "pain", "hurt", "bad day"
        ],
        "emojis": ["😢", "🥲", "💙"]
    },

    "angry": {
        "keywords": [
            "hate", "stupid", "annoying",
            "wtf", "damn", "idiot"
        ],
        "emojis": ["😤", "🙄", "😑"]
    },

    "gaming": {
        "keywords": [
            "game", "gaming", "minecraft",
            "valorant", "cs2", "elden",
            "boss", "level", "discord"
        ],
        "emojis": ["🎮", "🏆", "⚔️", "👾"]
    },

    "sleep": {
        "keywords": [
            "sleep", "bed", "night",
            "good night", "gn"
        ],
        "emojis": ["😴", "🌙", "💤"]
    },

    "greeting": {
        "keywords": [
            "hello", "hi", "hey",
            "morning", "afternoon", "evening"
        ],
        "emojis": ["👋", "😊", "☀️"]
    }
}


# ---------------------------------------
# Mood Detection
# ---------------------------------------

def detect_mood(text: str):

    text = text.lower()

    scores = {}

    for mood, data in MOODS.items():

        score = 0

        for keyword in data["keywords"]:
            if keyword in text:
                score += 1

        if score > 0:
            scores[mood] = score

    if not scores:
        return None

    highest = max(scores.values())

    winners = [
        mood
        for mood, score in scores.items()
        if score == highest
    ]

    return random.choice(winners)


# ---------------------------------------
# React
# ---------------------------------------

async def maybe_react(message: discord.Message):

    # Don't react every time
    if random.random() > REACTION_CHANCE:
        return

    mood = detect_mood(message.content)

    if mood is None:
        return

    emoji = random.choice(
        MOODS[mood]["emojis"]
    )

    try:
        await message.add_reaction(emoji)

    except discord.HTTPException:
        pass