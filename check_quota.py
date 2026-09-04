"""Report whether the LLM provider has room for a real agent call.

The rate limit headers describe only the per-minute bucket, so a small
request can succeed while the daily token quota is exhausted. This sends
a request the size of a real agent turn, which is the only reliable test.
"""
from bellwether.config import load_config
from groq import Groq

cfg = load_config()
client = Groq(api_key=cfg.secrets.groq_api_key)
try:
    client.chat.completions.create(
        model=cfg.agent["model"],
        max_tokens=200,
        messages=[{"role": "user", "content": "portfolio record " * 900}],
    )
    print("READY: a full-size agent call succeeded. Safe to run evaluation.")
except Exception as e:
    text = str(e)
    if "TPD" in text or "tokens per day" in text.lower():
        print("WAIT: the daily token quota is still exhausted.")
    else:
        print("BLOCKED for another reason:")
    print(text[:400])
