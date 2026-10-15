import os
from aiohttp import web
from .core import Core
from .server import create_app
from .state import State

state = State(os.getenv('DATA_DIR', '/data'))
web.run_app(create_app(state, Core(state)), host='0.0.0.0', port=int(os.getenv('PORT', '8000')), access_log=None)
