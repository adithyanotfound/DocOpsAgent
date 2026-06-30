import json
from openai import OpenAI
from app.core.config import settings

client = OpenAI(api_key=settings.openai_api_key)
system_prompt = (
    "You are a precise document editing assistant evaluating ONE specific text block at a time.\n"
    "You will be given an editing instruction, the text block itself, and its metadata.\n\n"
    "IMPORTANT RULES:\n"
    "1. You must decide if the provided text block is the INTENDED TARGET of the instruction.\n"
    "2. If the user asks to edit a 'title', 'heading', or 'topic', you MUST ASSUME the provided text block IS the target and rewrite it, UNLESS the text block is obviously a footer, slide number (like `<#>` or a plain digit), or a specific field label (like `Theme Name:`).\n"
    "3. If you decide the text block IS the target, return ONLY the new rewritten text.\n"
    "4. If you decide the text block is NOT the target, you MUST return the ORIGINAL TEXT EXACTLY AS-IS. Do not return any other text.\n"
    "5. Provide no commentary, markdown, or quotes."
)

user_prompt = (
    "Editing instruction: Change tht title of slide 2 to Solution\n\n"
    "Text block to consider:\nIdea/Approach Details\n\n"
    'Block metadata: {"slide": 2, "shape_name": "Unknown"}\n\n'
)
res = client.chat.completions.create(
    model=settings.llm_model,
    messages=[
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_prompt},
    ],
    temperature=0.3,
)
print('Result Title:', repr(res.choices[0].message.content.strip()))
