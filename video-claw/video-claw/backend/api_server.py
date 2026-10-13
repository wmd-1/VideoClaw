# -*- coding: utf-8 -*-
"""
Compatibility entrypoint for the Video-Claw FastAPI server.
"""

import os
import sys

_backend_dir = os.path.dirname(os.path.abspath(__file__))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

import uvicorn  # noqa: E402 - sys.path 引导后导入

from api.app import app  # noqa: E402 - sys.path 引导后导入
from config import settings  # noqa: E402 - sys.path 引导后导入


def main():
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, access_log=settings.ACCESS_LOG)


if __name__ == "__main__":
    main()
