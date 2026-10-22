import unittest
from unittest.mock import patch, MagicMock
import json
import os

# Set test environment
os.environ["ENV"] = "test"
os.environ["AUTO_DB_URI"] = "sqlite:///:memory:"

from src.integrations.operational_service import OperationalService

class TestApiDelayMonitoring(unittest.TestCase):
    def setUp(self):
        # Reset circuit breaker state
        OperationalService._consecutive_failures = 0
        OperationalService._is_circuit_broken = False

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.integrations.operational_service.OperationalService._refresh_brand_configs')
    @patch('src.integrations.operational_service.time.perf_counter')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_call_api_delay_alert(self, mock_save_log, mock_time, mock_refresh, mock_notify, mock_req):
        """测试 _call_api 在响应超过 5s 时触发告警"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "msg": "ok"}
        mock_resp.text = json.dumps(mock_resp.json.return_value)
        mock_req.return_value = mock_resp
        
        # 模拟时间流逝: 100.0 (start_time), 106.0 (end_time)
        mock_time.side_effect = [100.0, 106.0] 
        
        service = OperationalService()
        service._call_api("GET", "http://test-api.com/v1/test", "atlas")
        
        # 验证告警是否被调用
        alert_called = False
        for call in mock_notify.call_args_list:
            msg = call[0][0]
            if "响应过慢" in msg and "6.00s" in msg:
                alert_called = True
                break
        
        self.assertTrue(alert_called, "应当针对 6s 的延迟发送告警")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.integrations.operational_service.OperationalService._refresh_brand_configs')
    @patch('src.integrations.operational_service.time.perf_counter')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_call_api_no_delay_alert(self, mock_save_log, mock_time, mock_refresh, mock_notify, mock_req):
        """测试 _call_api 在响应正常（<5s）时不触发延迟告警"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 0, "msg": "ok"}
        mock_resp.text = json.dumps(mock_resp.json.return_value)
        mock_req.return_value = mock_resp
        
        # 模拟时间流逝: 100.0 (start_time), 101.0 (end_time)
        mock_time.side_effect = [100.0, 101.0] 
        
        service = OperationalService()
        service._call_api("GET", "http://test-api.com/v1/test", "atlas")
        
        # 验证没有发送“响应过慢”告警
        for call in mock_notify.call_args_list:
            msg = call[0][0]
            self.assertNotIn("响应过慢", msg, "不应针对 1s 的延迟发送告警")

if __name__ == '__main__':
    unittest.main()
