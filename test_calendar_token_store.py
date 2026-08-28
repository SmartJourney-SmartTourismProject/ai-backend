# test_calendar_token_store.py
# Run from your project root: python3 test_calendar_token_store.py
# No network calls - checks that get_stored_credentials/save_credentials
# actually persist to calendar_tokens.json and survive a "restart"
# (simulated here by re-importing the module fresh, since a real process
# restart can't happen inside one script).

import asyncio
import importlib
import os

from app.tools import calendar_tool
from app.tools.calendar_tool import _CREDENTIAL_STORE_PATH


async def main():
    checks = []

    # Clean slate so this test doesn't depend on prior runs.
    if _CREDENTIAL_STORE_PATH.exists():
        os.remove(_CREDENTIAL_STORE_PATH)

    # 1. Save credentials for a user.
    await calendar_tool.save_credentials("demo-user-1", {
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
    })

    # 2. The file should now exist on disk.
    checks.append(("calendar_tokens.json was created", _CREDENTIAL_STORE_PATH.exists()))

    # 3. Reading it back in the same process should work.
    creds = await calendar_tool.get_stored_credentials("demo-user-1")
    print("Read back (same process):", creds)
    checks.append((
        "Read back matches what was saved (same process)",
        creds == {"access_token": "fake-access-token", "refresh_token": "fake-refresh-token"}
    ))

    # 4. Simulate a restart: reload the module fresh (a real restart would
    # lose any in-memory dict, but should NOT lose what's on disk).
    reloaded = importlib.reload(calendar_tool)
    creds_after_reload = await reloaded.get_stored_credentials("demo-user-1")
    print("Read back (after simulated restart):", creds_after_reload)
    checks.append((
        "Credentials survive a simulated restart",
        creds_after_reload == {"access_token": "fake-access-token", "refresh_token": "fake-refresh-token"}
    ))

    # 5. A user with no stored credentials should get None, not crash.
    missing = await reloaded.get_stored_credentials("nobody-connected-yet")
    checks.append(("Unknown user_id returns None", missing is None))

    # 6. Saving for a second user shouldn't clobber the first.
    await reloaded.save_credentials("demo-user-2", {"access_token": "other-token", "refresh_token": None})
    still_there = await reloaded.get_stored_credentials("demo-user-1")
    checks.append(("Saving a second user doesn't overwrite the first", still_there is not None))

    passed = 0
    for label, ok in checks:
        if ok:
            passed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")

    print(f"\n{passed}/{len(checks)} checks passed")

    # Cleanup so this test file doesn't leave fake tokens lying around.
    if _CREDENTIAL_STORE_PATH.exists():
        os.remove(_CREDENTIAL_STORE_PATH)


if __name__ == "__main__":
    asyncio.run(main())
