# import os
# from google import genai
# from google.genai import types

# class GoogleADKClient:
#     def __init__(self, model_name="gemini-2.5-flash-lite"):
#         api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        
#         if not api_key:
#             raise ValueError("No API Key found. Please set GOOGLE_API_KEY in your environment.")
            
#         self.client = genai.Client(api_key=api_key)
#         self.model_name = model_name

#     def generate(self, system_prompt: str, user_prompt: str) -> str:
#         response = self.client.models.generate_content(
#             model=self.model_name,
#             contents=user_prompt,
#             config=types.GenerateContentConfig(
#                 system_instruction=system_prompt,
#                 temperature=0.2,
#                 response_mime_type="application/json",
#             )
#         )
#         return response.text

import os
import openai

class OpenAIClient:
    def __init__(self, model_name="gpt-4o-mini"):
        api_key = os.environ.get("OPENAI_API_KEY")
        
        if not api_key:
            raise ValueError("No API Key found. Please set OPENAI_API_KEY in your environment.")
            
        openai.api_key = api_key
        self.model_name = model_name

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        response = openai.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2
        )
        return response.choices[0].message.content
