"""Thin wrapper over the google-genai SDK (replaces the old Cloudflare calls)."""
from google import genai
from google.genai import types

import config

client = genai.Client(api_key=config.GEMINI_API_KEY)


async def generate(model: str, contents: list, system: str,
                   tools: list | None = None, allow_tools: bool = True,
                   temperature: float = 0.8, max_tokens: int = 700):
    """One Gemini call. Automatic function calling is DISABLED: our agent loop
    executes tools itself so that permissions/budgets are enforced in code."""
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    if tools:
        cfg.tools = [types.Tool(function_declarations=tools)]
        cfg.tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="AUTO" if allow_tools else "NONE"
            )
        )
    return await client.aio.models.generate_content(model=model, contents=contents, config=cfg)
