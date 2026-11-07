"""Google AI Studio channel using the shared Gemini native transport."""

from .gemini import GeminiProvider


class GeminiAIStudioProvider(GeminiProvider):
    """Explicit AI Studio channel with its own history metadata namespace."""

    provider_name = "gemini-aistudio"
    default_base_url = "https://generativelanguage.googleapis.com"
