from .genericHttpxClient import get_client

try:
    from .completion import completions
except ImportError:  # pragma: no cover - optional dependency support
    completions = None

try:
    from .chatCompletion import chat_completions
except ImportError:  # pragma: no cover - optional dependency support
    chat_completions = None

try:
    from .streamChatCompletion import streaming_chat_completions
except ImportError:  # pragma: no cover - optional dependency support
    streaming_chat_completions = None

__all__ = ["get_client", "completions", "chat_completions", "streaming_chat_completions"]
