from google import genai
from google.genai import types

from app.config import GEMINI_API_KEY, GEMINI_MODEL

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY is missing. Set it in app/services/.env locally or in your hosting environment.")

client = genai.Client(api_key=GEMINI_API_KEY)


def ask_llm(prompt, tools=None):
    return client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
