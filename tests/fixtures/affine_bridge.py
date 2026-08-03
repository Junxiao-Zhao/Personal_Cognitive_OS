from __future__ import annotations

import json
import sys


request = json.load(sys.stdin)
mapping = {
    page["entity_id"]: f"affine-page-{page['entity_id']}"
    for page in request["pages"]
}
json.dump({"ok": True, "memory_commit": request["memory_commit"], "mapping": mapping}, sys.stdout)
