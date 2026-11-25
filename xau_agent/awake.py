"""Hold the PC awake with the display on, so Modern Standby (S0 low-power idle) cannot suspend the bots.

On S0 machines the system enters standby when the screen turns off, and desktop programs are suspended there;
ES_SYSTEM_REQUIRED alone (what the bots request) does not stop that. ES_DISPLAY_REQUIRED keeps the screen on,
which keeps the PC out of standby. No Windows setting is changed: stop this process and normal behaviour returns.
Closing the lid or choosing Sleep still sleeps.

    python -m xau_agent.awake          # run until killed (pid in logs/awake.pid)
"""
from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path

ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002


def main() -> None:
    pid_file = Path(__file__).resolve().parent.parent / "logs" / "awake.pid"
    pid_file.write_text(str(os.getpid()))
    k = ctypes.windll.kernel32
    print(f"holding display + system awake: {bool(k.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED))}",
          flush=True)
    while True:
        time.sleep(60)
        k.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)


if __name__ == "__main__":
    main()
