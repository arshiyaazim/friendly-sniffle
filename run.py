#!/usr/bin/env python3
"""
Fazle Core entrypoint.
Usage: python run.py
"""
import uvicorn
from app.config import get_settings

if __name__ == "__main__":
    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=s.app_port,
        reload=s.debug,
        log_level=s.log_level.lower(),
        access_log=True,
    )
