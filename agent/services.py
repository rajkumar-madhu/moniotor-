"""Public helpers other apps may call."""
from .llm import configured, ping


def model_configured():
    return configured()


def test_connection():
    return ping()
