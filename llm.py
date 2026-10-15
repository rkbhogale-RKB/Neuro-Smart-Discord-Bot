"""Thin wrapper over the google-genai SDK (replaces the old Cloudflare calls)."""
import asyncio
import logging

from google import genai
from google.genai import errors, types

import config

log = logging.getLogger("llm")
client = genai.Client(api_key=config.GEMINI_API_KEY)

RETRYABLE = {429, 500, 502, 503, 504}


def _is_gemma(model: str) -> bool:
    return model.lower().startswith("gemma")


async def generate(model: str, contents: list, system: str,
                   tools: list | None = None, allow_tools: bool = True,
                   temperature: float = 0.8, max_tokens: int = 700):
    """One model call. Automatic function calling is DISABLED: our agent loop
    executes tools itself so permissions/budgets are enforced in code."""
    contents = list(contents)
    sys_arg = system
    if _is_gemma(model):
        # Gemma often rejects system_instruction: fold it into the first user turn.
        first = contents[0]
        intro = types.Part(text=f"[Instructions]\n{system}\n[End instructions]\n\n")
        contents[0] = types.Content(role="user", parts=[intro, *first.parts])
        sys_arg = None

    cfg = types.GenerateContentConfig(
        system_instruction=sys_arg,
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

    last_err = None
    for attempt in range(3):
        try:
            return await client.aio.models.generate_content(
                model=model, contents=contents, config=cfg)
        except errors.APIError as e:
            log.warning("Model %s error %s (attempt %d/3): %s",
                        model, getattr(e, "code", "?"), attempt + 1, str(e)[:200])
            if getattr(e, "code", None) not in RETRYABLE:
                raise
            last_err = e
            await asyncio.sleep(2 * (attempt + 1))
    raise last_err