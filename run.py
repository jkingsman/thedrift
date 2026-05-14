from aiohttp import web
from station.app import create_app

if __name__ == "__main__":
    app = create_app()
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    web.run_app(app, host="0.0.0.0", port=port)
