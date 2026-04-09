#!/usr/bin/env python3
"""
BTC Trading Bot - Entry Point

Usage:
    python run.py              # Run on default port 8000
    python run.py --port 8080  # Run on custom port
    python run.py --reload     # Run with auto-reload (development)
"""

import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="BTC Trading Bot")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")

    args = parser.parse_args()

    print(f"""
    ╔══════════════════════════════════════════╗
    ║         BTC Trading Bot v1.0.0           ║
    ║                                          ║
    ║  Signals: trdr.io integration            ║
    ║  Exchange: Bybit                         ║
    ╚══════════════════════════════════════════╝

    Starting server at http://{args.host}:{args.port}
    API Docs: http://{args.host}:{args.port}/docs
    """)

    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )


if __name__ == "__main__":
    main()
