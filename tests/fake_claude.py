"""Stands in for `claude -p --output-format stream-json` in tests. Echoes its argv and stdin length."""
import json
import os
import sys

prompt = sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "argv": sys.argv[1:],
                  "cycle_id": os.environ.get("XAU_CYCLE_ID"),
                  "has_api_key": "ANTHROPIC_API_KEY" in os.environ}))
print("not json noise")
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "NO TRADE"}]}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
                  "result": f"NO TRADE: spread too wide (prompt {len(prompt)} chars)",
                  "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 900}}))
