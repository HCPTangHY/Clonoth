"""Vertex-backed channel through a configured Gemini-compatible endpoint."""

from .gemini import GeminiProvider


class GeminiVertexProvider(GeminiProvider):
    """Explicit Vertex channel; the endpoint handles upstream routing and auth.

    ponytail: this adapter uses the existing /v1beta/models gateway contract.
    Direct Vertex project/location URLs and OAuth require a separate transport.
    """

    provider_name = "gemini-vertex"
    is_vertex = True
