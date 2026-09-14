#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地启动入口：python3 run.py [端口]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import init_db
from app.server import main

if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1:
        os.environ["PULP_PORT"] = sys.argv[1]
    os.environ.setdefault("PULP_PORT", "8050")
    main()
