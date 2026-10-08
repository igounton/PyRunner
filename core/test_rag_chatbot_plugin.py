"""Discover the private RAG plugin's tests when the plugin is present."""

from pathlib import Path

if (Path(__file__).resolve().parent.parent / "plugins/rag_chatbot/tests.py").exists():
    from plugins.rag_chatbot.tests import *  # noqa: F401,F403
