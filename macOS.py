import subprocess

from qobuz_common import IDLE, QOBUZ_IDLE_TITLE, BaseSynchronizer, run_app

LOCAL_VERSION = "1.0.0"
QOBUZ_APPLICATION_NAME = "Qobuz"

# Checks "is running" outside the tell block, so polling never launches Qobuz
APPLESCRIPT = f'''
if application "{QOBUZ_APPLICATION_NAME}" is running then
    tell application "{QOBUZ_APPLICATION_NAME}"
        try
            return (name of current track) & " - " & (artist of current track)
        on error
            return "{QOBUZ_IDLE_TITLE}"
        end try
    end tell
end if
return ""
'''


class MacSynchronizer(BaseSynchronizer):
    """Reads the current track from Qobuz via AppleScript."""

    def poll(self):
        try:
            result = subprocess.run(['osascript', '-e', APPLESCRIPT],
                                    capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            print("AppleScript command timed out.")
            return self.last_state
        except OSError as e:
            print(f"AppleScript execution failed: {e}")
            return None

        if result.returncode != 0:
            # Script errors (e.g. missing automation permission) while Qobuz is running count as idle
            return None if "isn't running" in result.stderr else IDLE

        title = result.stdout.strip()
        if not title:
            return None
        return IDLE if title == QOBUZ_IDLE_TITLE else title


if __name__ == '__main__':
    run_app(MacSynchronizer, LOCAL_VERSION, "macOS")
