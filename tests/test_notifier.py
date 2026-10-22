import unittest
from unittest.mock import patch, MagicMock

from src.runtime.notifier import send_feishu_notification


class TestNotifier(unittest.TestCase):
    @patch("src.runtime.notifier.os.getenv", return_value=None)
    def test_send_feishu_notification_returns_false_without_webhook(self, mock_getenv):
        self.assertFalse(send_feishu_notification("content"))

    @patch("src.runtime.notifier.requests.post")
    @patch("src.runtime.notifier.os.getenv", return_value="http://webhook")
    def test_send_feishu_notification_success(self, mock_getenv, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        self.assertTrue(send_feishu_notification("content", "标题"))

    @patch("src.runtime.notifier.requests.post")
    @patch("src.runtime.notifier.os.getenv", return_value="http://webhook")
    def test_send_feishu_notification_failure_and_exception(self, mock_getenv, mock_post):
        mock_post.return_value = MagicMock(status_code=500, text="bad")
        self.assertFalse(send_feishu_notification("content"))
        mock_post.side_effect = RuntimeError("network")
        self.assertFalse(send_feishu_notification("content"))


if __name__ == "__main__":
    unittest.main()
