"""LLM for the server/sim stack: one fixed short reply."""
from core.providers.llm.base import LLMProviderBase


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.reply = config.get("reply", "Okay, done.")

    def response(self, session_id, dialogue, **kwargs):
        yield self.reply
