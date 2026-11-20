# scripts/check_models.py
"""Find out which Gemini models actually respond for your key. Sends ONE tiny request per candidate.

    python scripts/check_models.py                              # the usual candidates
    python scripts/check_models.py gemini-2.5-flash other-id    # your own list

Each test uses one request of your free-tier quota (the delay between them keeps it under 10 per minute).
Put a working model's ID in manifests/deployment_audit.yaml (llm.planner_model and llm.tester_model).
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402
from google import genai  # noqa: E402
from google.genai import errors, types  # noqa: E402

load_dotenv()
key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
if not key:
    sys.exit("No GEMINI_API_KEY in .env")
client = genai.Client(api_key=key)

CANDIDATES = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]      # the two 500-a-day models; pass others by name
wanted = sys.argv[1:] or CANDIDATES

try:
    listed = sorted(m.name.removeprefix("models/") for m in client.models.list()
                    if "generateContent" in (m.supported_actions or []) and "gemini" in m.name)
    print(f"Models your key can see ({len(listed)}):\n  " + "\n  ".join(listed) + "\n")
except errors.APIError as e:
    print(f"Could not list models ({e.code} {e.status}): {e.message}\n")

print("Testing one request per model:")
for i, model in enumerate(wanted):
    if i:
        time.sleep(7)
    start = time.monotonic()
    try:
        r = client.models.generate_content(model=model, contents="Reply with the single word: ok",
                                           config=types.GenerateContentConfig(
                                               max_output_tokens=20, http_options=types.HttpOptions(timeout=30_000)))
        print(f"  OK     {model:28s} {time.monotonic() - start:4.1f}s  reply: {(r.text or '').strip()[:20]!r}")
    except errors.APIError as e:
        print(f"  FAIL   {model:28s} {time.monotonic() - start:4.1f}s  {e.code} {e.status}: {str(e.message)[:90]}")
    except Exception as e:
        print(f"  FAIL   {model:28s} {time.monotonic() - start:4.1f}s  {type(e).__name__}: {str(e)[:90]}")
