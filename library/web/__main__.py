"""python -m web: serve the media library page."""

import uvicorn

from web.app import create_app
from web.config import WebConfig

config = WebConfig.from_env()
uvicorn.run(create_app(config), host="0.0.0.0", port=config.port, proxy_headers=False,
            server_header=False, log_level="info")
