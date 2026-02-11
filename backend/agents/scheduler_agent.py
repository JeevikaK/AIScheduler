import json
from typing import List, Optional, Dict, Union
from agents.prompt import SCHEDULER_SYSTEM_PROMPT
from pydantic import BaseModel, Field
from services.google_adk import GoogleADKClient

class TaskArguments(BaseModel):
    title: str
    description: Optional[str]
    date: str
    explicit_time: Optional[str]
    duration_minutes: int
    priority: int

class AgentDecision(BaseModel):
    action: str
    arguments: Optional[Union[Dict, List[Dict]]] = None
    question: Optional[str] = None
    message: Optional[str] = None

class SchedulerAgent:
    def __init__(self, llm_client):
        """
        llm_client must implement:
        - generate(system_prompt: str, user_prompt: str) -> str
        """
        self.llm = llm_client

    def decide(self, user_text: str) -> AgentDecision:
        raw = self.llm.generate(
            system_prompt=SCHEDULER_SYSTEM_PROMPT,
            user_prompt=user_text,
        )
        try:
            parsed = json.loads(raw)
            return AgentDecision(**parsed)
        except Exception as e:
            # Fallback: fail safe
            return AgentDecision(
                action="respond",
                message="Sorry, I didn’t understand that. Can you rephrase?"
            )

llm_client = GoogleADKClient()
scheduler_agent = SchedulerAgent(llm_client)