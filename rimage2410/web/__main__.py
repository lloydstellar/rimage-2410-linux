"""Start the web interface: python -m rimage2410.web --host 0.0.0.0 --port 8080"""

import argparse
import os

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="Rimage 2410 web interface (dry run by default)")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow access from the LAN")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data-dir", default=os.environ.get("RIMAGE_DATA_DIR", "~/rimage-data"))
    ap.add_argument("--access-code", default=os.environ.get("RIMAGE_ACCESS_CODE", ""),
                    help="access code for the login (empty = no login)")
    ap.add_argument("--hardware", action="store_true",
                    help="drive the real 2410; starting a job counts as approval for moves that are known to work")
    ap.add_argument("--max-upload-mb", type=int, default=int(os.environ.get("RIMAGE_MAX_UPLOAD_MB", "2048")))
    args = ap.parse_args()
    app = create_app({"DATA_DIR": args.data_dir, "ACCESS_CODE": args.access_code,
                      "MAX_CONTENT_LENGTH": args.max_upload_mb * 1024 * 1024,
                      "HARDWARE": "real" if args.hardware else "dry-run"})
    mode = "REAL MACHINE" if args.hardware else "dry run"
    print(f"Rimage 2410 web interface on http://{args.host}:{args.port}  ({mode}, data directory {app.config['DATA_DIR']})")
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
