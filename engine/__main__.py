from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Axiom local trading engine; desktop launch is recommended")
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--data-dir",type=Path,default=Path.home()/".axiom")
    parser.add_argument("--token-stdin",action="store_true",help="Read {token:...} as one JSON line from stdin")
    args = parser.parse_args()
    if not args.token_stdin:
        parser.error("--token-stdin is required; the desktop shell securely provides a random session token")
    try:
        bootstrap = json.loads(sys.stdin.readline())
        token = bootstrap["token"]
        if not isinstance(token,str) or len(token) < 32:
            raise ValueError("invalid token")
    except Exception:
        parser.error("Expected a JSON bootstrap object with a random token of at least 32 characters")
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535; 0 selects an available port")
    import uvicorn
    from .api import create_app
    # The child owns its socket continuously. The desktop never reserves and
    # releases a port before handing a session token to an unidentified listener.
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:
        if sys.platform == "win32" and hasattr(socket,"SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        listener.bind(("127.0.0.1",args.port))
        listener.listen(128)
        listener.setblocking(False)
        bound_port=listener.getsockname()[1]
        print("AXIOM_READY " + json.dumps({"port":bound_port},separators=(",",":")),flush=True)
        config=uvicorn.Config(create_app(args.data_dir,token),host="127.0.0.1",port=bound_port,log_level="warning",access_log=False)
        uvicorn.Server(config).run(sockets=[listener])


if __name__ == "__main__":
    main()
