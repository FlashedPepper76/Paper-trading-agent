"""
One-off connectivity test for the Gemini integration — NOT part of the
trading pipeline. Calls ai_agent's Gemini function with a fake context so
we can verify auth + JSON parsing work without waiting for market hours.
Writes the result to last_gemini_test.json so it can be read back via the
GitHub Contents API.
"""
import json

import ai_agent

fake_context = {
    "account": {"equity": 100000.0, "cash": 100000.0},
    "held_positions": {},
    "watchlist": {
        "AAPL": {"last_close": 210.5, "pct_change_1d": 0.3, "pct_change_5d": 1.2,
                  "pct_change_10d": -0.5, "pct_change_30d": 4.1},
        "SPY": {"last_close": 560.2, "pct_change_1d": 0.1, "pct_change_5d": 0.8,
                 "pct_change_10d": 1.0, "pct_change_30d": 3.2},
    },
}

try:
    result = ai_agent._call_gemini(fake_context)
    output = {"success": True, "result": result}
except Exception as e:
    output = {"success": False, "error": str(e)}

with open("last_gemini_test.json", "w") as f:
    json.dump(output, f, indent=2)

print(json.dumps(output, indent=2))
