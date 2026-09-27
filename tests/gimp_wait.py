# gimp-console batch command for the end-to-end test: keeps GIMP (and
# with it the resident GIMP Link listener) running until the test
# writes the flag file TEST_DONE, or for at most 240 seconds.
#
# Copyright 2026 David
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import time

flag = os.environ["TEST_DONE"]
end = time.time() + 240
while time.time() < end and not os.path.exists(flag):
    time.sleep(0.1)
print("GIMP WAIT %s" % ("done" if os.path.exists(flag) else "timeout"), flush=True)
