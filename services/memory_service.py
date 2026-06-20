from collections import defaultdict

MAX_HISTORY = 20

# Stores conversation history per Discord channel
conversation_memory = defaultdict(list)


def add_message(channel_id: int, role: str, content: str):
    """
    Add a message to the conversation history.
    """

    conversation_memory[channel_id].append(
        {
            "role": role,
            "content": content,
        }
    )

    trim_history(channel_id)


def get_history(channel_id: int):
    """
    Return conversation history.
    """

    return conversation_memory[channel_id]


def trim_history(channel_id: int):
    """
    Keep only the most recent messages.
    """

    if len(conversation_memory[channel_id]) > MAX_HISTORY:
        conversation_memory[channel_id] = conversation_memory[channel_id][-MAX_HISTORY:]


def clear_history(channel_id: int):
    """
    Clear conversation history.
    """

    conversation_memory[channel_id].clear()