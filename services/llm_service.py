import os

import requests
from dotenv import load_dotenv

load_dotenv()

ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
MODEL = os.getenv("MODEL")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SYSTEM_PROMPT_PATH = os.path.join(
    BASE_DIR,
    "prompts",
    "system_prompt.txt"
)

with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read().strip()

URL = (
    f"https://api.cloudflare.com/client/v4/accounts/"
    f"{ACCOUNT_ID}/ai/run/{MODEL}"
)

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}


def generate_response(messages):
    """
    Send conversation to Cloudflare.
    """

    payload = {
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ] + messages
    }

    try:

        response = requests.post(
            URL,
            headers=HEADERS,
            json=payload,
            timeout=60,
        )

        response.raise_for_status()

        data = response.json()

        return data["result"]["response"]

    except Exception as e:

        print(e)

        return "Sorry, I'm having trouble thinking right now."