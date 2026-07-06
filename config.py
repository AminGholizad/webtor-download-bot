import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from SlotManager import SlotManager

# --- SETTINGS ---
MAX_CONCURRENT_DOWNLOADS = 3
ARIA2_RPC_URL = "http://localhost:6800/jsonrpc"
ARIA2_RPC_SECRET = "your_secure_token_here"  # Match the token used to start aria2c
# ----------------

# Lock to prevent file corruption during parallel status updates
file_modify_lock = threading.Lock()
# Shared reference to the executor so we can re-queue refreshed items
global_executor: Optional[ThreadPoolExecutor] = None
global_target_folder: str = "~/Downloads"


slot_manager = SlotManager(MAX_CONCURRENT_DOWNLOADS)
