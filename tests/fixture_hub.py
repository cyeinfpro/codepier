"""Start the real test Hub on an already-bound socket, without a port handoff."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socket

from hub.config import HubConfig
from shared.process_logging import configure_logging
from shared.util import atomic_json


def bind_listener(port=0):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    import uvicorn
    with bind_listener(args.port) as listener:
        port = listener.getsockname()[1]
        os.environ["HUB_DATA_DIR"] = str(Path(args.data_dir).resolve())
        os.environ["HUB_PORT"] = str(port)
        for key in ("HUB_PUBLIC_URL", "MCP_PUBLIC_URL"):
            if os.environ.get(key) in {None, "", "http://127.0.0.1:0"}:
                os.environ[key] = f"http://127.0.0.1:{port}"
        config = HubConfig.from_env()
        configure_logging(config.log_level)
        server = uvicorn.Server(uvicorn.Config(
            "hub.app:create_app", factory=True, host="127.0.0.1", port=port,
            workers=1, log_level=config.log_level, ws_max_size=16 * 1024 * 1024,
            proxy_headers=True, forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
            access_log=False, timeout_graceful_shutdown=10))
        atomic_json(args.receipt, {"port": port})
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
