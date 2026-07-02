from __future__ import annotations

import logging
import unittest

from bot.logging_conf import setup_logging


class TestLoggingConf(unittest.TestCase):
    def test_httpx_never_logs_below_warning_even_if_app_level_is_debug(self):
        # httpx/httpcore log full request URLs at INFO, including query-string
        # secrets like an API key — must be suppressed regardless of app log level.
        setup_logging("DEBUG", "data/test_logging_conf.log")
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
