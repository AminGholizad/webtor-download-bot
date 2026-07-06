import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from SlotManager import SlotManager

# --- SETTINGS ---
MAX_CONCURRENT_DOWNLOADS = 3
# ----------------

# Lock to prevent file corruption during parallel status updates
file_modify_lock = threading.Lock()
# Shared reference to the executor so we can re-queue refreshed items
global_executor: Optional[ThreadPoolExecutor] = None
global_target_folder: str = "~/Downloads"


slot_manager = SlotManager(MAX_CONCURRENT_DOWNLOADS)
