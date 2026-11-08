"""Process-group cleanup must never throw on macOS permission races."""
from pathlib import Path
import signal
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps/mcp-server'))
from lifecycle import terminate_group


class Termination(unittest.TestCase):
    def test_group_permission_error_falls_back_without_crashing(self):
        process = Mock(pid=12345)
        process.poll.return_value = 0
        with patch('lifecycle.os.killpg', side_effect=PermissionError):
            self.assertFalse(terminate_group(process, grace=0))
        self.assertEqual([call.args[0] for call in process.send_signal.call_args_list],
                         [signal.SIGTERM, signal.SIGKILL])

    def test_missing_group_is_already_clean(self):
        process = Mock(pid=12345)
        process.poll.return_value = 0
        with patch('lifecycle.os.killpg', side_effect=ProcessLookupError):
            self.assertTrue(terminate_group(process, grace=0))
        process.send_signal.assert_not_called()
