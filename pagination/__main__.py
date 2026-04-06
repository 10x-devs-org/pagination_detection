"""
__main__.py — allows running the package with: python -m pagination
"""

import asyncio
from .cli import main

asyncio.run(main())
