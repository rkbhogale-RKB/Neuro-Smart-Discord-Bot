from collections import defaultdict

MAX_HISTORY = 20

# Stores conversation history for each Discord channel
conversation_memory = defaultdict(list)


def add_message(channel_id: int, role: str, content: str):
    """
    Add a message to channel history.
    """

    conversation_memory[channel_id].append(
        {
            "role": role,
            "content": content
        }
    )

    trim_history(channel_id)


def get_history(channel_id: int):
    """
    Return conversation history for a channel.
    """

    return conversation_memory[channel_id]


def trim_history(channel_id: int):
    """
    Keep only the latest MAX_HISTORY messages.
    """

    if len(conversation_memory[channel_id]) > MAX_HISTORY:
        conversation_memory[channel_id] = conversation_memory[channel_id][-MAX_HISTORY:]


def clear_history(channel_id: int):
    """
    Clear a channel's conversation history.
    """

    conversation_memory[channel_id].clear()