import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from dotenv import load_dotenv
from SlotManager import SlotManager
import os
# Load the environment variables from the .env file
load_dotenv()

# --- SETTINGS ---
MAX_CONCURRENT_DOWNLOADS = 3
ARIA2_RPC_HOST = "127.0.0.1"
ARIA2_RPC_PORT = 6800
ARIA2_RPC_URL = f"http://{ARIA2_RPC_HOST}:{ARIA2_RPC_PORT}/jsonrpc"
ARIA2_RPC_SECRET = os.environ.get("ARIA2_RPC_SECRET")
# ----------------

# Lock to prevent file corruption during parallel status updates
file_modify_lock = threading.Lock()
# Shared reference to the executor so we can re-queue refreshed items
global_executor: Optional[ThreadPoolExecutor] = None
global_target_folder: str = "~/Downloads"


slot_manager = SlotManager(MAX_CONCURRENT_DOWNLOADS)
