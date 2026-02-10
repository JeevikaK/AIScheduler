import os
from google import genai
from google.genai import types

class GoogleADKClient:
    def __init__(self, model_name="gemini-2.5-flash-lite"):  # Updated to a 2.0+ stable model
        # The new SDK looks for GEMINI_API_KEY by default, 
        # but we'll manually pull GOOGLE_API_KEY if that's what you're using.
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        
        if not api_key:
            raise ValueError("No API Key found. Please set GOOGLE_API_KEY in your environment.")
            
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        # Note: The new SDK uses 'contents' (plural) for the main prompt
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.2,
                response_mime_type="application/json",
            )
        )
        return response.text