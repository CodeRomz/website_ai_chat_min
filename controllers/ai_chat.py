# -*- coding: utf-8 -*-
from __future__ import annotations

from odoo import http, tools, _
from odoo.http import request

import json
import time
import re as re_std
import logging
from typing import Dict, List, Tuple, Optional, Callable, Any

_logger = logging.getLogger(__name__)

