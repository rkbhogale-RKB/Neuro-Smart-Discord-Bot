import asyncio, os
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
models = [os.getenv("ROUTER_MODEL", "gemini-2.5-flash-lite"),
          os.getenv("ANSWER_MODEL", "gemini-2.5-flash")]
tool = types.Tool(function_declarations=[types.FunctionDeclaration(
    name="remember_fact", description="save a fact",
    parameters_json_schema={"type": "object",
        "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
        "required": ["key", "value"]})])

async def main():
    try:
        print("Models your key can use:")
        async for m in await client.aio.models.list():
            print("  ", m.name)
    except Exception as e:
        print("could not list models:", str(e)[:150])
    for model in models:
        for label, cfg in [("plain", None),
                           ("with tool", types.GenerateContentConfig(tools=[tool]))]:
            try:
                r = await client.aio.models.generate_content(
                    model=model, contents="say hi in 3 words", config=cfg)
                print("OK  ", model, label, "->", r.text or r.function_calls)
            except Exception as e:
                print("FAIL", model, label, "->", str(e)[:150])

asyncio.run(main())