import unittest
import os
import ast
import hashlib
from pathlib import Path
from unittest.mock import MagicMock, call, patch
import time
import json
import requests
import src.integrations.operational_service
from src.integrations.operational_service import OperationalService, SummitService
from src.integrations.business_http import (
    BusinessCircuitOpen,
    BusinessRequestGovernor,
    BusinessRequestLimiterUnavailable,
    CircuitRegistry,
)
from contextlib import contextmanager


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TestServicesExtended(unittest.TestCase):
    def setUp(self):
        OperationalService.brand_configs.clear()
        OperationalService.brand_configs["seed"] = {"apiKey": "k", "tenantId": 1}
        SummitService._shared_token = None
        env_patch = patch.dict(
            os.environ,
            {"SUMMIT_OAUTH_TOKEN": "test-oauth-basic"},
            clear=False,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    @patch('src.integrations.operational_service.requests.post')
    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_summit_service_full(self, mock_save_log, mock_req, mock_post):
        service = SummitService("test")

        # 1. Token logic with expiry
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"access_token": "T1", "expires_in": 10})
        token = service.get_token()
        self.assertEqual(token, "T1")
        self.assertTrue(service._token_expires_at < time.time() + 10)

        # Force expiry
        service._token_expires_at = time.time() - 1
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"access_token": "T2", "expires_in": 3600})
        token2 = service.get_token()
        self.assertEqual(token2, "T2")

        # 2. Search products
        mock_req.return_value = MagicMock(status_code=200, json=lambda: {"datas": {"dsmList": [{"dsmCode": "C1", "models": [{"webLabel": "L1", "price": 100, "media": {"images": [""]}}]}]}})
        res = service.search_products("q")
        self.assertEqual(len(res), 1)

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_search_can_skip_per_model_detail_enrichment(self, mock_request):
        service = SummitService("test")
        service.get_token = MagicMock(return_value="token")
        service.get_product_details = MagicMock(side_effect=AssertionError("detail lookup should be skipped"))
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "datas": {
                    "dsmList": [
                        {
                            "dsmCode": "C1",
                            "family": "冲锋衣",
                            "models": [
                                {
                                    "modelCode": "M1",
                                    "webLabel": "防水夹克",
                                    "media": {"images": ["https://img.example/m1.jpg"]},
                                }
                            ],
                        }
                    ]
                }
            },
        )

        result = service.search_products_with_status("冲锋衣", enrich_details=False)

        self.assertTrue(result["success"])
        self.assertEqual(result["products"][0]["productId"], "M1")
        service.get_product_details.assert_not_called()

    @patch.dict(os.environ, {"LUMEN_API_KEY": "lumen-test-secret"}, clear=False)
    def test_lumen_common_brand_headers(self):
        service = OperationalService.__new__(OperationalService)
        service.env = "test"
        self.assertEqual(service._get_api_alias("lumen"), "alias_lumen")
        self.assertEqual(service._get_headers("lumen")["tenant-id"], "243")

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_business_requests_use_shared_governor(self, mock_request):
        acquired_keys = []

        class RecordingGovernor:
            @contextmanager
            def acquire(self, key, request_timeout):
                acquired_keys.append((key, request_timeout))
                yield

            def request_with_rate_limit_retry(
                self, *, key, method, url, request_func, request_timeout, **kwargs
            ):
                kwargs.pop("max_attempts", None)
                with self.acquire(key, request_timeout):
                    return request_func(method, url, **kwargs)

        service = SummitService("test")
        service.request_governor = RecordingGovernor()
        service.get_token = MagicMock(return_value="token")
        mock_request.return_value = MagicMock(status_code=200, json=lambda: {})

        service._request(
            "POST",
            "https://api-pp.summit.example.test/search-engine/searchops/textSearchNew/v2",
            json={"text": "背包"},
            timeout=15,
        )

        self.assertEqual(
            acquired_keys,
            [("test|api-pp.summit.example.test|client:summit", 15.0)],
        )

    @patch('src.integrations.operational_service.requests.post')
    def test_summit_oauth_request_uses_shared_governor(self, mock_post):
        recorded_keys = []

        class RecordingGovernor:
            def request_with_rate_limit_retry(
                self, *, key, method, url, request_func, **kwargs
            ):
                recorded_keys.append(key)
                kwargs.pop("max_attempts", None)
                kwargs.pop("request_timeout", None)
                return request_func(method, url, **kwargs)

        service = SummitService("test", request_governor=RecordingGovernor())
        mock_post.return_value = MagicMock(
            status_code=200,
            headers={},
            json=lambda: {"access_token": "oauth-token", "expires_in": 3600},
        )

        assert service.get_token() == "oauth-token"
        assert len(recorded_keys) == 1
        assert recorded_keys[0].startswith(
            "test|preprod.idp.summit.example.test|client:summit-oauth:"
        )
        assert service.auth_basic not in recorded_keys[0]

    @patch('src.integrations.operational_service.requests.post')
    def test_summit_oauth_request_places_grant_type_in_query(self, mock_post):
        service = SummitService("test")
        mock_post.return_value = MagicMock(
            status_code=200,
            headers={},
            json=lambda: {"access_token": "oauth-token", "expires_in": 3600},
        )

        assert service.get_token() == "oauth-token"
        kwargs = mock_post.call_args.kwargs
        assert kwargs["params"] == {"grant_type": "client_credentials"}
        assert "data" not in kwargs

    @patch('src.integrations.operational_service.requests.post')
    def test_summit_oauth_retries_429_through_governor(self, mock_post):
        clock = FakeClock()
        service = SummitService("test")
        service.max_retries = 2
        service.request_governor = BusinessRequestGovernor(
            clock=clock.time,
            sleep=clock.sleep,
        )
        service.circuit_registry = CircuitRegistry(clock=clock.time)
        mock_post.side_effect = [
            MagicMock(status_code=429, headers={"Retry-After": "2"}, text="limited"),
            MagicMock(
                status_code=200,
                headers={},
                text='{"access_token":"oauth-token"}',
                json=lambda: {"access_token": "oauth-token", "expires_in": 3600},
            ),
        ]

        assert service.get_token() == "oauth-token"
        assert mock_post.call_count == 2
        assert clock.sleeps == [2.0]
        assert service.request_governor.metrics()["rateLimitRetryCount"] == 1

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_business_request_retries_429_with_retry_after(self, mock_request):
        clock = FakeClock()
        service = SummitService("test")
        service.get_token = MagicMock(return_value="token")
        service.request_governor = BusinessRequestGovernor(
            clock=clock.time,
            sleep=clock.sleep,
        )
        mock_request.side_effect = [
            MagicMock(status_code=429, headers={"Retry-After": "2"}),
            MagicMock(status_code=200, headers={}, json=lambda: {}),
        ]

        response = service._request(
            "POST",
            "https://api-pp.summit.example.test/search-engine/searchops/textSearchNew/v2",
            json={"text": "背包"},
            timeout=15,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(clock.sleeps, [2.0])

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_replaces_explicit_none_timeout_and_returns_permit_on_success(self, mock_request):
        permit = object()
        circuit = MagicMock()
        circuit.admit.return_value = permit
        service = SummitService("test", circuit_registry=circuit)
        service.get_token = MagicMock(return_value="token")
        mock_request.return_value = MagicMock(
            status_code=200,
            headers={},
            json=lambda: {"datas": {"dsmList": []}},
        )

        service._request(
            "POST",
            "https://api-pp.summit.example.test/search-engine/searchops/textSearchNew/v2",
            json={"text": "背包"},
            timeout=None,
        )

        self.assertEqual(mock_request.call_args.kwargs["timeout"], 15.0)
        circuit.record_success.assert_called_once_with(
            "test|api-pp.summit.example.test|client:summit",
            permit,
        )

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_401_retry_network_exception_records_one_failure(self, mock_request):
        service = SummitService("test")
        service.get_token = MagicMock(side_effect=["old-token", "new-token"])
        mock_request.side_effect = [
            MagicMock(status_code=401, headers={}, json=lambda: {"code": 401}),
            requests.RequestException("second request failed"),
        ]

        with self.assertRaisesRegex(requests.RequestException, "second request failed"):
            service._request(
                "POST",
                "https://api-pp.summit.example.test/search-engine/searchops/textSearchNew/v2",
                json={"text": "背包"},
            )

        status = service.circuit_registry.status(
            "test|api-pp.summit.example.test|client:summit"
        )
        self.assertEqual(status["consecutive_failures"], 1)

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_malformed_200_records_failure_and_preserves_parse_error(self, mock_request):
        service = SummitService("test")
        service.get_token = MagicMock(return_value="token")
        response = MagicMock(status_code=200, headers={}, text="not-json")
        response.json.side_effect = ValueError("invalid json")
        mock_request.return_value = response

        with self.assertRaisesRegex(ValueError, "invalid json"):
            service._request(
                "POST",
                "https://api-pp.summit.example.test/search-engine/searchops/textSearchNew/v2",
                json={"text": "背包"},
            )

        status = service.circuit_registry.status(
            "test|api-pp.summit.example.test|client:summit"
        )
        self.assertEqual(status["consecutive_failures"], 1)

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_nested_malformed_search_records_circuit_failure(self, mock_request):
        service = SummitService("test")
        service.get_token = MagicMock(return_value="token")
        mock_request.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"datas":[]}',
            json=lambda: {"datas": []},
        )

        result = service.search_products_with_status("背包", page_size=16)

        self.assertFalse(result["success"])
        status = service.circuit_registry.status(
            "test|api-pp.summit.example.test|client:summit"
        )
        self.assertEqual(status["consecutive_failures"], 1)

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_business_error_fails_search_without_availability_failure(self, mock_request):
        payloads = [
            {"resp_code": 500, "datas": {"dsmList": []}},
            {"resp_code": 500},
        ]

        for payload in payloads:
            with self.subTest(payload=payload):
                service = SummitService("test")
                service.get_token = MagicMock(return_value="token")
                mock_request.return_value = MagicMock(
                    status_code=200,
                    headers={},
                    text=json.dumps(payload),
                    json=lambda payload=payload: payload,
                )

                result = service.search_products_with_status("背包", page_size=16)

                self.assertFalse(result["success"])
                status = service.circuit_registry.status(
                    "test|api-pp.summit.example.test|client:summit"
                )
                self.assertEqual(status["consecutive_failures"], 0)

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_top_level_list_200_is_malformed_and_records_failure(self, mock_request):
        service = SummitService("test")
        service.get_token = MagicMock(return_value="token")
        mock_request.return_value = MagicMock(
            status_code=200,
            headers={},
            text="[]",
            json=lambda: [],
        )

        with self.assertRaisesRegex(ValueError, "JSON must be an object"):
            service._request(
                "POST",
                "https://api-pp.summit.example.test/search-engine/searchops/textSearchNew/v2",
                json={"text": "背包"},
            )

        status = service.circuit_registry.status(
            "test|api-pp.summit.example.test|client:summit"
        )
        self.assertEqual(status["consecutive_failures"], 1)

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_business_request_respects_identity_circuit(self, mock_request):
        class OpenCircuit:
            def admit(self, key):
                return None

        service = SummitService("test")
        service.get_token = MagicMock(return_value="token")
        service.circuit_registry = OpenCircuit()

        with self.assertRaises(BusinessCircuitOpen):
            service._request(
                "POST",
                "https://api-pp.summit.example.test/search-engine/searchops/textSearchNew/v2",
                json={"text": "背包"},
                timeout=15,
            )

        mock_request.assert_not_called()

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_business_request_records_availability_failure(self, mock_request):
        service = SummitService("test")
        service.get_token = MagicMock(return_value="token")
        service.circuit_registry = CircuitRegistry(failure_threshold=2, cooldown_seconds=60)
        mock_request.return_value = MagicMock(status_code=503, headers={})

        service._request(
            "POST",
            "https://api-pp.summit.example.test/search-engine/searchops/textSearchNew/v2",
            json={"text": "背包"},
            timeout=15,
        )

        status = service.circuit_registry.status(
            "test|api-pp.summit.example.test|client:summit"
        )
        self.assertEqual(status["consecutive_failures"], 1)

    def test_summit_blacklist_update_rejects_successful_empty_detail(self):
        service = SummitService("test")
        service.get_product_details_with_status = MagicMock(
            return_value={"success": True, "products": [], "error_reason": None}
        )
        service._request = MagicMock()

        updated = service.update_blacklist("MC001", ["word1"])

        self.assertFalse(updated)
        service._request.assert_not_called()

    def test_summit_blacklist_update_rejects_mismatched_detail(self):
        service = SummitService("test")
        service.get_product_details_with_status = MagicMock(
            return_value={
                "success": True,
                "products": [
                    {
                        "productId": "summit_MC999",
                        "modelCode": "MC999",
                        "blackListAi": "existing",
                    }
                ],
                "error_reason": None,
            }
        )
        service._request = MagicMock()

        updated = service.update_blacklist("MC001", ["word1"])

        self.assertFalse(updated)
        service._request.assert_not_called()

    @patch("src.integrations.operational_service.save_operation_log")
    def test_summit_blacklist_update_preserves_matching_detail_blacklist(
        self, mock_save_operation_log
    ):
        service = SummitService("test")
        service.get_product_details_with_status = MagicMock(
            return_value={
                "success": True,
                "products": [
                    {
                        "productId": "summit_MC001",
                        "blackListAi": "existing",
                    }
                ],
                "error_reason": None,
            }
        )
        service._request = MagicMock(
            return_value=MagicMock(
                status_code=200,
                text='{"code":0}',
                json=lambda: {"code": 0},
            )
        )

        updated = service.update_blacklist("MC001", ["word1"])

        self.assertTrue(updated)
        request_payload = service._request.call_args.kwargs["json"]
        self.assertEqual(request_payload["productId"], "summit_MC001")
        self.assertEqual(set(request_payload["blackListAi"].split(",")), {"existing", "word1"})
        mock_save_operation_log.assert_called_once()

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_all_brands(self, mock_save_log, mock_req):
        service = OperationalService()
        brands = ["atlas", "meadow", "harbor", "riviera"]
        for b in brands:
            OperationalService.brand_configs[b] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["lxptkkhe"] = {"apiKey": "k", "tenantId": 1}

        mock_req.return_value = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"rows": [{"productId": "S1"}]}})

        for b in brands:
            res = service.search_products(b, "test")
            self.assertEqual(len(res), 1)

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_uses_total_field(self, mock_save_log, mock_req):
        service = OperationalService()
        brand = "atlas"
        OperationalService.brand_configs[brand] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}

        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"total": 128, "rows": [{"productId": "S1"}]}},
        )

        result = service.search_products_with_total(brand, "连衣裙", page_size=16)

        self.assertTrue(result["success"])
        self.assertEqual(result["total_count"], 128)
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["error_reason"])
        self.assertEqual(result["products"], [{"productId": "S1"}])

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_accepts_bounded_request_policy(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"total": 3, "rows": []}},
        )

        result = service.search_products_with_total(
            "atlas",
            "连衣裙",
            page_size=16,
            request_timeout=3,
            max_attempts=1,
        )

        self.assertTrue(result["success"])
        self.assertEqual(mock_req.call_args.kwargs["timeout"], 3)

    @patch('src.integrations.operational_service.time.sleep')
    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_bounded_policy_does_not_retry_inside_governor(
        self, mock_save_log, mock_req, mock_sleep,
    ):
        service = OperationalService()
        service.max_retries = 3
        mock_req.return_value = MagicMock(status_code=503, headers={}, text="unavailable")

        result = service.search_products_with_total(
            "atlas", "连衣裙", page_size=16, request_timeout=3, max_attempts=1,
        )

        self.assertFalse(result["success"])
        self.assertEqual(mock_req.call_count, 1)
        mock_sleep.assert_not_called()

    def test_search_all_products_with_total_aggregates_every_page(self):
        service = OperationalService()
        first_page = {
            "success": True,
            "products": [{"productId": f"P{index}"} for index in range(1, 17)],
            "total_count": 18,
            "result_complete": False,
            "error_reason": None,
        }
        second_page = {
            "success": True,
            "products": [{"productId": "P17"}, {"productId": "P18"}],
            "total_count": 18,
            "result_complete": False,
            "error_reason": None,
        }
        service.search_products_with_total = MagicMock(side_effect=[first_page, second_page])

        result = service.search_all_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertTrue(result["success"])
        self.assertTrue(result["result_complete"])
        self.assertEqual(result["total_count"], 18)
        self.assertEqual([item["productId"] for item in result["products"]], [f"P{index}" for index in range(1, 19)])
        self.assertEqual(service.search_products_with_total.call_args_list, [
            call("atlas", "连衣裙", page_size=16, page_num=1),
            call("atlas", "连衣裙", page_size=16, page_num=2),
        ])

    def test_search_all_products_with_total_fails_closed_on_changed_total(self):
        service = OperationalService()
        service.search_products_with_total = MagicMock(side_effect=[
            {
                "success": True,
                "products": [{"productId": f"P{index}"} for index in range(1, 17)],
                "total_count": 18,
                "result_complete": False,
                "error_reason": None,
            },
            {
                "success": True,
                "products": [{"productId": "P17"}, {"productId": "P18"}],
                "total_count": 19,
                "result_complete": False,
                "error_reason": None,
            },
        ])

        result = service.search_all_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertEqual(result["error_reason"], "total_changed_between_pages")

    def test_search_all_products_with_total_rejects_duplicate_products(self):
        service = OperationalService()
        service.search_products_with_total = MagicMock(side_effect=[
            {
                "success": True,
                "products": [{"productId": f"P{index}"} for index in range(1, 17)],
                "total_count": 18,
                "result_complete": False,
                "error_reason": None,
            },
            {
                "success": True,
                "products": [{"productId": "P16"}, {"productId": "P17"}],
                "total_count": 18,
                "result_complete": False,
                "error_reason": None,
            },
        ])

        result = service.search_all_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertEqual(result["error_reason"], "duplicate_search_product")

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.persistence.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_riviera_search_accepts_string_zero_code_without_failed_operation_log(
        self,
        mock_req,
        mock_save_log,
        mock_notify,
    ):
        service = OperationalService()
        service.max_retries = 1
        mock_req.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":"0","data":{"total":1,"rows":[{"productId":"RL-1"}]}}',
            json=lambda: {
                "code": "0",
                "data": {"total": 1, "rows": [{"productId": "RL-1"}]},
            },
        )

        result = service.search_products_with_total(
            "riviera",
            "polo",
            page_size=16,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["products"], [{"productId": "RL-1"}])
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(
            [call.args[0]["status"] for call in mock_save_log.call_args_list],
            ["success"],
        )
        mock_notify.assert_not_called()

    @patch('src.integrations.operational_service.time.sleep')
    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.persistence.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_search_rejects_string_business_error_code(
        self,
        mock_req,
        mock_save_log,
        mock_notify,
        mock_sleep,
    ):
        service = OperationalService()
        service.max_retries = 1
        mock_req.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":"500","data":{"total":0,"rows":[]}}',
            json=lambda: {"code": "500", "data": {"total": 0, "rows": []}},
        )

        result = service.search_products_with_total(
            "riviera",
            "polo",
            page_size=16,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_reason"], "search_retries_exhausted")
        self.assertEqual(mock_req.call_count, 1)
        self.assertEqual(
            [call.args[0]["status"] for call in mock_save_log.call_args_list],
            ["failed"],
        )
        mock_notify.assert_called_once()
        mock_sleep.assert_called_once_with(1)

    @patch('src.integrations.operational_service.time.sleep')
    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.persistence.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_repeated_search_business_errors_do_not_open_availability_circuit(
        self,
        mock_req,
        mock_save_log,
        mock_notify,
        mock_sleep,
    ):
        service = OperationalService()
        service.max_retries = 1
        service.circuit_registry = CircuitRegistry(
            failure_threshold=2,
            cooldown_seconds=60,
        )
        mock_req.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":"500","data":{"total":0,"rows":[]}}',
            json=lambda: {"code": "500", "data": {"total": 0, "rows": []}},
        )

        results = [
            service.search_products_with_total(
                "riviera",
                "polo",
                page_size=16,
            )
            for _ in range(3)
        ]

        request_key = service._business_request_key(
            "riviera",
            f"{service._get_base_url('riviera')}/api/search/mixed",
        )
        circuit_status = service.circuit_registry.status(request_key)
        self.assertTrue(all(not result["success"] for result in results))
        self.assertEqual(mock_req.call_count, 3)
        self.assertEqual(circuit_status["consecutive_failures"], 0)
        self.assertEqual(circuit_status["state"], "closed")
        self.assertEqual(
            [call.args[0]["status"] for call in mock_save_log.call_args_list],
            ["failed", "failed", "failed"],
        )
        self.assertEqual(mock_notify.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 3)

    @patch('src.integrations.operational_service.time.sleep')
    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.persistence.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_repeated_invalid_boolean_codes_open_availability_circuit(
        self,
        mock_req,
        mock_save_log,
        mock_notify,
        mock_sleep,
    ):
        service = OperationalService()
        service.max_retries = 1
        service.circuit_registry = CircuitRegistry(
            failure_threshold=2,
            cooldown_seconds=60,
        )
        mock_req.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":true,"data":{"total":0,"rows":[]}}',
            json=lambda: {"code": True, "data": {"total": 0, "rows": []}},
        )

        results = [
            service.search_products_with_total(
                "riviera",
                "polo",
                page_size=16,
            )
            for _ in range(3)
        ]

        request_key = service._business_request_key(
            "riviera",
            f"{service._get_base_url('riviera')}/api/search/mixed",
        )
        circuit_status = service.circuit_registry.status(request_key)
        self.assertEqual(mock_req.call_count, 2)
        self.assertEqual(circuit_status["consecutive_failures"], 2)
        self.assertEqual(circuit_status["state"], "open")
        self.assertEqual(
            [result["error_reason"] for result in results],
            [
                "search_retries_exhausted",
                "search_retries_exhausted",
                "circuit_breaker_open",
            ],
        )
        self.assertEqual(
            [call.args[0]["status"] for call in mock_save_log.call_args_list],
            ["failed", "failed", "circuit_breaker_open"],
        )
        self.assertEqual(mock_notify.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_search_response_validator_does_not_bypass_structure_for_business_error(self):
        self.assertFalse(
            OperationalService._is_valid_search_response({"code": "500"})
        )

    def test_search_success_code_normalization_is_strict(self):
        for response_code in (None, 0, 200, "0", " 0 ", "200", " 200 "):
            with self.subTest(response_code=response_code):
                self.assertTrue(
                    OperationalService._is_success_response_code(response_code)
                )

        for response_code in (False, True, -1, 1, 500, 0.0, 200.0, "", "00", "500"):
            with self.subTest(response_code=response_code):
                self.assertFalse(
                    OperationalService._is_success_response_code(response_code)
                )

    def test_business_error_code_normalization_is_strict(self):
        checker = getattr(
            OperationalService,
            "_is_business_error_response_code",
            None,
        )
        self.assertIsNotNone(checker)

        for response_code in (-1, 1, 500, "-1", "500", " error "):
            with self.subTest(response_code=response_code):
                self.assertTrue(checker(response_code))

        for response_code in (
            None,
            False,
            True,
            0,
            200,
            "0",
            " 200 ",
            "",
            "   ",
            0.0,
            500.0,
            [],
            {},
        ):
            with self.subTest(response_code=response_code):
                self.assertFalse(checker(response_code))

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_marks_complete_zero_result_as_trustworthy(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"total": 0, "rows": []}},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertTrue(result["success"])
        self.assertTrue(result["result_complete"])
        self.assertEqual(result["total_count"], 0)
        self.assertIsNone(result["error_reason"])

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_circuit_isolated_by_upstream_identity(self, mock_save_log, mock_request):
        class SelectiveCircuitRegistry:
            def admit(self, key):
                return None if key.endswith("tenant:178") else object()

            def record_success(self, key, permit):
                return None

            def record_failure(self, key, permit):
                return None

            def record_rate_limited(self, key, permit):
                return None

        service = OperationalService()
        service.circuit_registry = SelectiveCircuitRegistry()
        mock_request.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":0,"data":{"total":1,"rows":[{"productId":"SKU-1"}]}}',
            json=lambda: {
                "code": 0,
                "data": {"total": 1, "rows": [{"productId": "SKU-1"}]},
            },
        )

        riviera = service.search_products_with_total("riviera", "polo", page_size=16)
        atlas = service.search_products_with_total("atlas", "dress", page_size=16)

        self.assertFalse(riviera["success"])
        self.assertEqual(riviera["error_reason"], "circuit_breaker_open")
        self.assertTrue(atlas["success"])
        self.assertEqual(mock_request.call_count, 1)

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_without_trusted_total_is_incomplete(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"rows": [{"productId": "S1"}]}},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertTrue(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertIsNone(result["error_reason"])

    @patch('src.integrations.operational_service.time.sleep')
    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_rejects_malformed_total_without_retrying(self, mock_save_log, mock_req, mock_sleep):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"total": "not-a-number", "rows": []}},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertEqual(mock_req.call_count, 1)
        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_nested_malformed_200_records_circuit_failure(self, mock_save_log, mock_req):
        service = OperationalService()
        mock_req.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":0,"data":[]}',
            json=lambda: {"code": 0, "data": []},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_reason"], "invalid_search_response")
        request_key = service._business_request_key(
            "atlas",
            f"{service._get_base_url('atlas')}/api/search/mixed",
        )
        self.assertEqual(
            service.circuit_registry.status(request_key)["consecutive_failures"],
            1,
        )

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_rejects_negative_total(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"total": -1, "rows": []}},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_rejects_total_smaller_than_returned_rows(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"total": 0, "rows": [{"productId": "S1"}]}},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["products"], [])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_rejects_non_list_rows(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"total": 1, "rows": {"productId": "S1"}}},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["products"], [])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_rejects_list_data(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": []},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertEqual(mock_req.call_count, 1)
        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_rejects_null_data(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": None},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertEqual(mock_req.call_count, 1)
        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_rejects_missing_data(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertEqual(mock_req.call_count, 1)
        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.integrations.operational_service.save_operation_log')
    def test_summit_search_products_with_total_is_incomplete_without_trusted_total(self, mock_save_log):
        service = OperationalService()
        service.summit_service.search_products_with_status = MagicMock(return_value={
            "success": True,
            "products": [{"productId": "D1"}],
            "error_reason": None,
        })

        result = service.search_products_with_total("summit", "背包", page_size=16)

        self.assertTrue(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertIsNone(result["error_reason"])

    @patch('src.integrations.operational_service.save_operation_log')
    def test_summit_search_forwards_bounded_live_request_policy(self, mock_save_log):
        service = OperationalService()
        service.summit_service.search_products_with_status = MagicMock(return_value={
            "success": True,
            "products": [{"productId": "D1"}],
            "error_reason": None,
        })

        result = service.search_products_with_total(
            "summit",
            "冲锋衣",
            page_size=16,
            enrich_details=False,
            request_timeout=2.5,
            max_attempts=1,
        )

        self.assertTrue(result["success"])
        service.summit_service.search_products_with_status.assert_called_once_with(
            "冲锋衣",
            16,
            enrich_details=False,
            request_timeout=2.5,
            max_attempts=1,
        )

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_summit_structured_results_do_not_touch_legacy_global_circuit(self, mock_save_log, mock_notify):
        service = OperationalService()
        service.failure_threshold = 2
        service.summit_service.search_products_with_status = MagicMock(side_effect=[
            {"success": False, "products": [], "error_reason": "search_exception:TimeoutError"},
            {"success": True, "products": [], "error_reason": None},
            {"success": False, "products": [], "error_reason": "search_exception:TimeoutError"},
        ])

        first = service.search_products_with_total("summit", "背包", page_size=16)
        successful = service.search_products_with_total("summit", "背包", page_size=16)
        third = service.search_products_with_total("summit", "背包", page_size=16)

        self.assertFalse(first["success"])
        self.assertTrue(successful["success"])
        self.assertFalse(third["success"])
        mock_notify.assert_not_called()

    @patch('src.integrations.operational_service.save_operation_log')
    def test_summit_real_search_timeout_is_reported_as_failed(self, mock_save_log):
        service = OperationalService()
        service.summit_service._request = MagicMock(side_effect=TimeoutError("upstream timeout"))

        result = service.search_products_with_total("summit", "背包", page_size=16)

        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["products"], [])
        self.assertEqual(result["error_reason"], "search_exception:TimeoutError")

    @patch('src.integrations.operational_service.save_operation_log')
    def test_summit_real_search_empty_result_is_successful_but_incomplete(self, mock_save_log):
        service = OperationalService()
        service.summit_service._request = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {"datas": {"dsmList": []}},
        ))

        result = service.search_products_with_total("summit", "不存在商品", page_size=16)

        self.assertTrue(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["products"], [])
        self.assertIsNone(result["error_reason"])

    @patch('src.integrations.operational_service.save_operation_log')
    def test_summit_search_exception_is_not_reported_as_empty_success(self, mock_save_log):
        service = OperationalService()
        service.summit_service._search_products_or_raise = MagicMock(side_effect=RuntimeError("upstream unavailable"))

        result = service.search_products_with_total("summit", "背包", page_size=16)

        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["error_reason"], "search_exception:RuntimeError")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_rejects_non_dict_product(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {"code": 0, "data": {"total": 1, "rows": ["SKU-1"]}},
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["products"], [])
        self.assertEqual(result["error_reason"], "invalid_search_response")

    @patch('src.integrations.operational_service.time.sleep')
    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_marks_retry_exhaustion_as_failed(self, mock_save_log, mock_req, mock_sleep):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(status_code=503)

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertEqual(mock_req.call_count, service.max_retries)
        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["error_reason"], "search_retries_exhausted")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_total_reports_rate_limited_after_429_retries(
        self,
        mock_save_log,
        mock_request,
    ):
        clock = FakeClock()
        service = OperationalService()
        service.max_retries = 2
        service.request_governor = BusinessRequestGovernor(
            clock=clock.time,
            sleep=clock.sleep,
        )
        mock_request.return_value = MagicMock(
            status_code=429,
            headers={"Retry-After": "0"},
            text="limited",
        )

        result = service.search_products_with_total("atlas", "连衣裙", page_size=16)

        self.assertEqual(mock_request.call_count, 2)
        self.assertFalse(result["success"])
        self.assertFalse(result["result_complete"])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["products"], [])
        self.assertEqual(result["error_reason"], "rate_limited")
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(
            service.request_governor.metrics()["structuredFailureCount"],
            1,
        )

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_search_products_with_bucket_snapshot_uses_raw_keyword_without_gender_split(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "code": 0,
                "data": {
                    "total": 2,
                    "rows": [
                        {"productId": "SKU1", "skuId": "SKU1", "ext": {"bucket": "productIdsByNoStock"}},
                        {"productId": "SKU2", "skuId": "SKU2", "ext": {"bucket": "productIdsByOpsLowWeight"}},
                    ],
                },
            },
        )

        result = service.search_products_with_bucket_snapshot("atlas", "皮衣 男士", page_size=50)

        payload = mock_req.call_args.kwargs["json"]
        self.assertEqual(payload["keyword"], "皮衣 男士")
        self.assertNotIn("filters", payload)
        self.assertNotIn("gender", payload)
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["products"][0]["bucket"], "productIdsByNoStock")
        self.assertEqual(result["products"][1]["sku_id"], "SKU2")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_current_ops_low_weight_skus_filters_bucket_snapshot(self, mock_save_log, mock_req):
        service = OperationalService()
        OperationalService.brand_configs["atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "code": 0,
                "data": {
                    "total": 2,
                    "rows": [
                        {"productId": "SKU1", "skuId": "SKU1", "ext": {"bucket": "productIdsByNoStock"}},
                        {"productId": "SKU2", "skuId": "SKU2", "ext": {"bucket": "productIdsByOpsLowWeight"}},
                    ],
                },
            },
        )

        self.assertEqual(service.get_current_ops_low_weight_skus("atlas", "皮衣"), ["SKU2"])

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_bucket_snapshot_reports_rate_limit_as_unknown_instead_of_zero(
        self,
        mock_save_log,
        mock_request,
    ):
        service = OperationalService()
        service.max_retries = 1
        mock_request.return_value = MagicMock(
            status_code=429,
            headers={"Retry-After": "0"},
            text="limited",
        )

        result = service.search_products_with_bucket_snapshot("atlas", "皮衣")

        self.assertFalse(result["success"])
        self.assertEqual(result["products"], [])
        self.assertIsNone(result["total_count"])
        self.assertEqual(result["error_reason"], "rate_limited")

    def test_get_current_ops_low_weight_skus_rejects_unavailable_search_evidence(self):
        service = OperationalService()
        service.search_products_with_bucket_snapshot = MagicMock(return_value={
            "success": False,
            "products": [],
            "total_count": None,
            "error_reason": "circuit_breaker_open",
        })

        with self.assertRaisesRegex(RuntimeError, "circuit_breaker_open"):
            service.get_current_ops_low_weight_skus("atlas", "皮衣")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_bucket_snapshot_failure_counts_structured_failure_once(
        self,
        mock_save_log,
        mock_request,
    ):
        service = OperationalService()
        service.max_retries = 1
        mock_request.return_value = MagicMock(
            status_code=429,
            headers={"Retry-After": "0"},
            text="limited",
        )

        with self.assertRaisesRegex(RuntimeError, "rate_limited"):
            service.get_current_ops_low_weight_skus("atlas", "皮衣")

        self.assertEqual(
            service.request_governor.metrics()["structuredFailureCount"],
            1,
        )

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_add_to_blacklist_all_brands(self, mock_save_log, mock_req):
        service = OperationalService()
        brands = ["atlas", "meadow", "harbor", "riviera"]
        for b in brands:
            OperationalService.brand_configs[b] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["lxptkkhe"] = {"apiKey": "k", "tenantId": 1}
        
        mock_req.return_value = MagicMock(status_code=200, json=lambda: {"code": 0})
        
        for b in brands:
            res = service.add_to_blacklist(b, "S1", ["Q1 女士"])
            self.assertTrue(res)

        request_payload = mock_req.call_args.kwargs["json"]
        self.assertEqual(request_payload["channelId"], "default")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_product_details_spu_payload(self, mock_save_log, mock_req):
        service = OperationalService()
        brand = "atlas"
        OperationalService.brand_configs[brand] = {"apiKey": "k", "tenantId": 1}
        
        mock_req.return_value = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": [{"spu": "S1"}]}})
        
        # Call with an ID
        service.get_product_details(brand, "C12345")
        
        # Verify the payload sent to requests.request
        args, kwargs = mock_req.call_args
        payload = kwargs.get('json', {})
        self.assertEqual(payload.get("spu"), "C12345")
        self.assertNotIn("brand", payload)
        self.assertNotIn("productName", payload)

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_product_status_map_batches_spu_with_semicolon(self, mock_save_log, mock_req):
        service = OperationalService()
        brand = "atlas"
        OperationalService.brand_configs[brand] = {"apiKey": "k", "tenantId": 1}
        mock_req.return_value = MagicMock(status_code=200, json=lambda: {
            "code": 0,
            "data": {
                "list": [
                    {"spu": "S1", "productId": "P1", "skuId": "K1", "status": "ON_SHELF"},
                    {"spuId": "S2", "status": "OFF_SHELF"},
                ]
            }
        })

        status_map = service.get_product_status_map(brand, ["S1", "S2"])

        args, kwargs = mock_req.call_args
        payload = kwargs.get("json", {})
        self.assertEqual(payload["spu"], "S1;S2")
        self.assertEqual(payload["pageSize"], 2)
        self.assertTrue(status_map["S1"]["exists"])
        self.assertTrue(status_map["P1"]["exists"])
        self.assertTrue(status_map["K1"]["is_online"])
        self.assertFalse(status_map["S2"]["is_online"])

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_product_status_map_chunks_requests_to_respect_page_size_limit(self, mock_save_log, mock_req):
        service = OperationalService()
        brand = "atlas"
        OperationalService.brand_configs[brand] = {"apiKey": "k", "tenantId": 1}

        def _response_for(spu_value):
            rows = [{"spu": spu, "status": "ON_SHELF"} for spu in spu_value.split(";") if spu]
            return MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": rows}})

        mock_req.side_effect = [
            _response_for(";".join(f"S{i}" for i in range(1, 101))),
            _response_for(";".join(f"S{i}" for i in range(101, 106))),
        ]

        status_map = service.get_product_status_map(brand, [f"S{i}" for i in range(1, 106)])

        first_payload = mock_req.call_args_list[0].kwargs["json"]
        second_payload = mock_req.call_args_list[1].kwargs["json"]
        self.assertEqual(first_payload["pageSize"], 100)
        self.assertEqual(second_payload["pageSize"], 5)
        self.assertEqual(len(status_map), 105)
        self.assertTrue(status_map["S105"]["exists"])

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_product_status_map_uses_smaller_chunks_and_longer_timeout_for_riviera(self, mock_save_log, mock_req):
        service = OperationalService()
        brand = "riviera"
        OperationalService.brand_configs[brand] = {"apiKey": "k", "tenantId": 1}
        OperationalService.brand_configs["lxptkkhe"] = {"apiKey": "k", "tenantId": 1}

        def _response_for(spu_value):
            rows = [{"spu": spu, "status": "ON_SHELF"} for spu in spu_value.split(";") if spu]
            return MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": rows}})

        mock_req.side_effect = [
            _response_for(";".join(f"R{i}" for i in range(1, 51))),
            _response_for(";".join(f"R{i}" for i in range(51, 56))),
        ]

        status_map = service.get_product_status_map(brand, [f"R{i}" for i in range(1, 56)])

        first_kwargs = mock_req.call_args_list[0].kwargs
        second_kwargs = mock_req.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["json"]["pageSize"], 50)
        self.assertEqual(second_kwargs["json"]["pageSize"], 5)
        self.assertEqual(first_kwargs["timeout"], 20)
        self.assertEqual(second_kwargs["timeout"], 20)
        self.assertEqual(len(status_map), 55)
        self.assertTrue(status_map["R55"]["exists"])

    def test_summit_product_status_map_returns_unknown_when_detail_query_is_unavailable(self):
        class OpenCircuit:
            def admit(self, key):
                return None

        service = OperationalService()
        service.summit_service.get_token = MagicMock(return_value="token")
        service.summit_service.circuit_registry = OpenCircuit()

        status_map = service.get_product_status_map("summit", ["8604201"])

        self.assertIsNone(status_map)

    @patch('src.integrations.operational_service.requests.request')
    def test_summit_product_status_map_distinguishes_successful_empty_result(self, mock_request):
        service = OperationalService()
        service.summit_service.get_token = MagicMock(return_value="token")
        mock_request.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":0,"data":{"list":[],"total":0}}',
            json=lambda: {"code": 0, "data": {"list": [], "total": 0}},
        )

        status_map = service.get_product_status_map("summit", ["8604201"])

        self.assertEqual(status_map, {})

    def test_calculate_broken_rate(self):
        service = OperationalService()
        broken_rate = service.calculate_broken_rate([
            {"name": "XS", "availability": "in_stock"},
            {"name": "S", "availability": "in_stock"},
            {"name": "M", "availability": "out_of_stock"},
            {"name": "L", "availability": "out_of_stock"},
        ])

        self.assertEqual(broken_rate, 0.5)

    def test_calculate_broken_rate_treats_low_on_stock_as_sellable(self):
        service = OperationalService()
        broken_rate = service.calculate_broken_rate([
            {"name": "XS", "availability": "in_stock"},
            {"name": "S", "availability": "low_on_stock"},
            {"name": "M", "availability": "out_of_stock"},
            {"name": "L", "availability": "coming_soon"},
        ])

        self.assertEqual(broken_rate, 0.5)

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_atlas_broken_rate(self, mock_save_log, mock_request):
        service = OperationalService()
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "detail": {
                    "colors": [
                        {
                            "sizes": [
                                {"name": "XS", "availability": "in_stock"},
                                {"name": "S", "availability": "low_on_stock"},
                            ]
                        }
                    ]
                }
            },
        )

        self.assertEqual(service.get_atlas_broken_rate("P1"), 0.0)

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_atlas_size_inventory_preserves_size_availability_detail(self, mock_save_log, mock_request):
        service = OperationalService()
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "detail": {
                    "colors": [
                        {
                            "sizes": [
                                {"name": "XS", "availability": "out_of_stock"},
                                {"name": "S", "availability": "in_stock"},
                                {"name": "M", "availability": "low_on_stock"},
                                {"name": "L", "availability": "coming_soon"},
                            ]
                        }
                    ]
                }
            },
        )

        inventory = service.get_product_size_inventory("atlas", {"productId": "C08727094700", "attr1": "498354604"})

        self.assertEqual(inventory["source"], "atlas_detail_api")
        self.assertEqual(inventory["total_size_count"], 4)
        self.assertEqual(inventory["available_size_count"], 2)
        self.assertEqual(inventory["broken_size_count"], 2)
        self.assertEqual(inventory["broken_rate"], 0.5)
        self.assertEqual(inventory["available_sizes"], ["S", "M"])
        self.assertEqual(inventory["unavailable_sizes"], ["XS", "L"])
        self.assertEqual(inventory["low_stock_sizes"], ["M"])
        self.assertEqual(inventory["size_rows"][0]["name"], "XS")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_atlas_size_inventory_skips_non_numeric_sku_without_detail_id(self, mock_save_log, mock_request):
        service = OperationalService()

        inventory = service.get_product_size_inventory("atlas", {"productId": "C08727094700"})

        self.assertIsNone(inventory)
        mock_request.assert_not_called()
        self.assertEqual(mock_save_log.call_args.args[0]["status"], "skipped")
        self.assertIn("missing_numeric_atlas_detail_id", mock_save_log.call_args.args[0]["error"])

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_atlas_size_inventory_logs_non_200_detail_response(self, mock_save_log, mock_request):
        service = OperationalService()
        mock_request.return_value = MagicMock(status_code=403, text="forbidden")

        inventory = service.get_atlas_size_inventory("498354604")

        self.assertIsNone(inventory)
        log_data = mock_save_log.call_args.args[0]
        self.assertEqual(log_data["brand"], "atlas")
        self.assertEqual(log_data["module"], "OperationalService")
        self.assertEqual(log_data["action"], "GET atlas_size_detail")
        self.assertEqual(log_data["status"], "failed")
        self.assertIn('"product_id": "498354604"', log_data["request"])
        self.assertIn('"status_code": 403', log_data["response"])

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_harbor_broken_rate(self, mock_save_log, mock_request):
        service = OperationalService()
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "variant": [
                        {"code": "SKU1", "store": 2},
                        {"code": "SKU2", "store": 0},
                        {"code": "SKU3", "store": 5},
                    ]
                }
            },
        )

        self.assertEqual(service.get_harbor_broken_rate("A123"), 0.3333)

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_harbor_size_inventory_preserves_variant_stock_detail(self, mock_save_log, mock_request):
        service = OperationalService()
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "variant": [
                        {"code": "SKU-S", "size": "S", "store": 2},
                        {"code": "SKU-M", "sizeName": "M", "store": 0},
                        {"code": "SKU-L", "name": "L", "store": 1},
                    ]
                }
            },
        )

        inventory = service.get_product_size_inventory("harbor", {"productId": "A123"})

        self.assertEqual(inventory["source"], "harbor_goods_items_api")
        self.assertEqual(inventory["total_size_count"], 3)
        self.assertEqual(inventory["available_size_count"], 2)
        self.assertEqual(inventory["broken_size_count"], 1)
        self.assertEqual(inventory["available_sizes"], ["S", "L"])
        self.assertEqual(inventory["unavailable_sizes"], ["M"])
        self.assertEqual(inventory["size_rows"][1]["code"], "SKU-M")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_harbor_size_inventory_parses_current_variant_fields(self, mock_save_log, mock_request):
        """线上 HARBOR goods/items 接口的尺码字段是 spec_value_name + store。"""

        service = OperationalService()
        mock_request.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "variant": [
                        {
                            "item_id": "2854000", "spec_id": "2", "spec_value_id": "313",
                            "spec_name": "size", "spec_value_name": "155/64A (34)",
                            "store": 2, "approve_status": "onsale", "is_show": "1",
                            "item_bn": "1349502001017", "instock": "1",
                        },
                        {
                            "item_id": "2854000", "spec_id": "2", "spec_value_id": "314",
                            "spec_name": "size", "spec_value_name": "170/76A (40)",
                            "store": 0, "approve_status": "onsale", "is_show": "1",
                            "item_bn": "1349502001018", "instock": "0",
                        },
                    ]
                }
            },
        )

        inventory = service.get_product_size_inventory("harbor", {"productId": "1349502001"})

        self.assertEqual(inventory["source"], "harbor_goods_items_api")
        self.assertEqual(inventory["total_size_count"], 2)
        self.assertEqual(inventory["available_size_count"], 1)
        self.assertEqual(inventory["broken_size_count"], 1)
        self.assertEqual(inventory["available_sizes"], ["155/64A (34)"])
        self.assertEqual(inventory["unavailable_sizes"], ["170/76A (40)"])

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_riviera_broken_rate(self, mock_save_log, mock_request):
        service = OperationalService()
        search_response = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "list": [
                        {
                            "skuList": [
                                {"skuCode": "SKU1"},
                                {"skuCode": "SKU2"},
                                {"skuCode": "SKU3"},
                            ]
                        }
                    ]
                }
            },
        )
        inventory_response = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "SKU1": {"netQty": 10},
                    "SKU2": {"netQty": 0},
                    "SKU3": {"netQty": 3},
                }
            },
        )

        mock_request.side_effect = [search_response, inventory_response]

        self.assertEqual(service.get_riviera_broken_rate("SPU123"), 0.3333)

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.persistence.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_riviera_net_qty_string_zero_does_not_send_failure_alert(
        self,
        mock_request,
        mock_save_log,
        mock_notify,
    ):
        service = OperationalService()
        search_response = MagicMock(
            status_code=200,
            headers={},
            text='{"data":{"list":[{"skuList":[{"skuCode":"MNPOKNI1N824080"}]}]}}',
            json=lambda: {
                "data": {
                    "list": [{"skuList": [{"skuCode": "MNPOKNI1N824080"}]}],
                },
            },
        )
        inventory_response = MagicMock(
            status_code=200,
            headers={},
            text='{"code":"0","data":{"successList":[{"extentionCode":"MNPOKNI1N824080","netQty":"2"}]}}',
            json=lambda: {
                "code": "0",
                "data": {
                    "successList": [{
                        "extentionCode": "MNPOKNI1N824080",
                        "netQty": "2",
                    }],
                },
            },
        )
        mock_request.side_effect = [search_response, inventory_response]

        self.assertEqual(service.get_riviera_broken_rate("MNPOKNI1N824080"), 0.0)
        self.assertEqual(
            [call.args[0]["status"] for call in mock_save_log.call_args_list],
            ["success", "success"],
        )
        mock_notify.assert_not_called()

    @patch('src.integrations.operational_service.requests.request')
    def test_get_riviera_broken_rate_supports_live_response_shape(self, mock_request):
        service = OperationalService()
        search_response = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "list": [
                        {
                            "skuList": [
                                {"code": "SKU1"},
                                {"externalSkuCode": "SKU2"},
                                {"code": "SKU3"},
                            ]
                        }
                    ]
                }
            },
        )
        inventory_response = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "successList": [
                        {"extentionCode": "SKU1", "netQty": "1"},
                        {"extentionCode": "SKU2", "netQty": "0"},
                        {"extentionCode": "SKU3", "netQty": "2"},
                    ]
                }
            },
        )

        mock_request.side_effect = [search_response, inventory_response]

        self.assertEqual(service.get_riviera_broken_rate("SPU123"), 0.3333)

    @patch('src.integrations.operational_service.requests.request')
    def test_get_riviera_size_inventory_preserves_sku_stock_detail(self, mock_request):
        service = OperationalService()
        search_response = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "list": [
                        {
                            "skuList": [
                                {"code": "SKU-S", "sizeName": "S"},
                                {"externalSkuCode": "SKU-M", "size": "M"},
                                {"skuCode": "SKU-L", "name": "L"},
                            ]
                        }
                    ]
                }
            },
        )
        inventory_response = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "successList": [
                        {"extentionCode": "SKU-S", "netQty": "1"},
                        {"extentionCode": "SKU-M", "netQty": "0"},
                        {"skuCode": "SKU-L", "netQty": "3"},
                    ]
                }
            },
        )

        mock_request.side_effect = [search_response, inventory_response]

        inventory = service.get_product_size_inventory("riviera", {"productId": "SPU123"})

        self.assertEqual(inventory["source"], "riviera_pdp_inventory_api")
        self.assertEqual(inventory["total_size_count"], 3)
        self.assertEqual(inventory["available_size_count"], 2)
        self.assertEqual(inventory["broken_size_count"], 1)
        self.assertEqual(inventory["available_sizes"], ["S", "L"])
        self.assertEqual(inventory["unavailable_sizes"], ["M"])
        self.assertEqual(inventory["size_rows"][0]["code"], "SKU-S")

    @patch('src.integrations.operational_service.requests.request')
    def test_get_riviera_broken_rate_selects_matching_spu_row(self, mock_request):
        service = OperationalService()
        search_response = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "list": [
                        {
                            "skuList": [
                                {"itemCode": "OTHER", "code": "OTHER001ONE"},
                            ]
                        },
                        {
                            "skuList": [
                                {"itemCode": "TARGET", "code": "TARGET001ONE"},
                                {"itemCode": "TARGET", "code": "TARGET002ONE"},
                            ]
                        },
                    ]
                }
            },
        )
        inventory_response = MagicMock(
            status_code=200,
            json=lambda: {
                "data": {
                    "successList": [
                        {"extentionCode": "TARGET001ONE", "netQty": "2"},
                        {"extentionCode": "TARGET002ONE", "netQty": "1"},
                    ]
                }
            },
        )

        mock_request.side_effect = [search_response, inventory_response]

        self.assertEqual(service.get_riviera_broken_rate("TARGET"), 0.0)
        sent_payload = mock_request.call_args_list[1].kwargs["json"]
        self.assertEqual(sent_payload["skuCodeList"], ["TARGET001ONE", "TARGET002ONE"])

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_product_details_exhaustive(self, mock_save_log, mock_req):
        service = OperationalService()
        brands = ["harbor", "meadow", "riviera"]
        for b in brands:
            OperationalService.brand_configs[b] = {"apiKey": "k", "tenantId": 1}
        
        mock_req.return_value = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": [{"spu": "S1"}]}})
        
        for b in brands:
            res = service.get_product_details(b, "test")
            self.assertEqual(len(res), 1)

    @patch('src.integrations.operational_service.save_operation_log')
    def test_hardcoded_brand_configs(self, mock_save_log):
        with patch.dict(os.environ, {
            "ATLAS_API_KEY": "atlas-key",
            "HARBOR_API_KEY": "harbor-key",
            "RIVIERA_API_KEY": "rl-key",
        }, clear=False), patch('src.integrations.operational_service.OperationalService.__init__', lambda self: None):
            service = OperationalService()
            service.env = "test"
            service.base_url = "http://test-api.example.com"
            service.brand_configs = {}
            
            self.assertEqual(service._get_api_alias("atlas"), "alias_atlas")
            self.assertEqual(service._get_base_url("atlas"), "http://staging.example.test")
            headers = service._get_headers("atlas")
            self.assertEqual(headers["Authorization"], "Bearer atlas-key")
            self.assertEqual(headers["tenant-id"], "235")

            service.env = "prod"
            self.assertEqual(service._get_api_alias("atlas"), "alias_atlas")
            self.assertEqual(service._get_base_url("atlas"), "http://search.example.test")
            headers = service._get_headers("atlas")
            self.assertEqual(headers["Authorization"], "Bearer atlas-key")
            self.assertEqual(headers["tenant-id"], "235")

            service.env = "test"
            self.assertEqual(service._get_api_alias("harbor"), "vbnmenzb")
            self.assertEqual(service._get_base_url("harbor"), "http://staging.example.test")
            headers = service._get_headers("harbor")
            self.assertEqual(headers["Authorization"], "Bearer harbor-key")
            self.assertEqual(headers["tenant-id"], "204")

            service.env = "prod"
            self.assertEqual(service._get_api_alias("harbor"), "vbnmenzb")
            headers = service._get_headers("harbor")
            self.assertEqual(headers["Authorization"], "Bearer harbor-key")
            self.assertEqual(headers["tenant-id"], "204")

            service.env = "test"
            self.assertEqual(service._get_api_alias("riviera"), "lxptkkhe")
            self.assertEqual(service._get_base_url("riviera"), "http://staging.example.test")
            headers = service._get_headers("riviera")
            self.assertEqual(headers["Authorization"], "Bearer rl-key")
            self.assertEqual(headers["tenant-id"], "178")

            service.env = "prod"
            self.assertEqual(service._get_api_alias("riviera"), "lxptkkhe")
            headers = service._get_headers("riviera")
            self.assertEqual(headers["Authorization"], "Bearer rl-key")
            self.assertEqual(headers["tenant-id"], "178")

            self.assertEqual(service._get_base_url("other"), "http://test-api.example.com")

    @patch('src.integrations.operational_service.save_operation_log')
    def test_get_runtime_status_and_clear_brand_config_cache(self, mock_save_log):
        OperationalService.brand_configs["cached"] = {"apiKey": "k", "tenantId": 1}

        runtime = OperationalService.get_runtime_status()
        self.assertEqual(runtime["consecutive_failures"], 0)
        self.assertFalse(runtime["is_circuit_broken"])
        OperationalService.clear_brand_config_cache()

        self.assertEqual(OperationalService.brand_configs, {})

    @patch.dict(os.environ, {"ENV": "test"}, clear=False)
    @patch('src.integrations.operational_service.OperationalService._refresh_brand_configs')
    def test_test_base_url_defaults_to_stg(self, mock_refresh):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_BASE_URL", None)
            service = OperationalService()
            self.assertEqual(service.base_url, "http://staging.example.test")

    def test_test_env_blocks_writes_to_prod_url(self):
        with patch('src.integrations.operational_service.OperationalService.__init__', lambda self: None):
            service = OperationalService()
            service.env = "test"
            with self.assertRaises(RuntimeError):
                service._guard_test_write_url("POST", "http://search.example.test/api/search/redis/setOpsLowWeight")
            service._guard_test_write_url("GET", "http://search.example.test/admin-api/common/brands/public")

    def test_runtime_status_reads_shared_limiter_and_circuit_telemetry(self):
        clock = FakeClock()
        governor = BusinessRequestGovernor(
            clock=clock.time,
            sleep=clock.sleep,
        )
        governor.request_with_rate_limit_retry(
            key="test|staging.example.test|tenant:235",
            method="POST",
            url="http://staging.example.test/api/search/mixed",
            request_func=lambda method, url: MagicMock(
                status_code=429,
                headers={"Retry-After": "0"},
            ),
            max_attempts=1,
            request_timeout=15,
        )
        registry = CircuitRegistry(
            clock=clock.time,
            failure_threshold=1,
            cooldown_seconds=60,
        )
        circuit_key = "test|staging.example.test|tenant:178"
        permit = registry.admit(circuit_key)
        self.assertIsNotNone(permit)
        registry.record_failure(circuit_key, permit)

        runtime = OperationalService.get_runtime_status()

        self.assertTrue(runtime["is_circuit_broken"])
        self.assertEqual(runtime["limiter"]["rateLimitedResponses"], 1)
        self.assertEqual(runtime["circuit"]["openCircuitCount"], 1)
        self.assertNotIn("openKeys", runtime["circuit"])
        self.assertEqual(clock.sleeps, [])

    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_success_business_success(self, mock_request, mock_save_log):
        service = OperationalService()
        mock_request.return_value = MagicMock(status_code=200, text='{"code":0}', json=lambda: {"code": 0})

        resp = service._call_api("POST", "http://staging.example.test/api/demo", "atlas", "q", json={"a": 1})

        self.assertEqual(resp.status_code, 200)

    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_injects_default_timeout_when_caller_omits_it(self, mock_request, mock_save_log):
        service = OperationalService()
        mock_request.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":0}',
            json=lambda: {"code": 0},
        )

        service._call_api(
            "GET",
            "http://staging.example.test/admin-api/common/brands/public",
            "system",
            "refresh_configs",
        )

        self.assertEqual(mock_request.call_args.kwargs["timeout"], 15.0)

    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_replaces_explicit_none_timeout_and_returns_permit_on_success(self, mock_request, mock_save_log):
        permit = object()
        circuit = MagicMock()
        circuit.admit.return_value = permit
        service = OperationalService()
        service.circuit_registry = circuit
        mock_request.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":0}',
            json=lambda: {"code": 0},
        )

        service._call_api(
            "GET",
            "http://staging.example.test/admin-api/common/brands/public",
            "system",
            "refresh_configs",
            timeout=None,
        )

        self.assertEqual(mock_request.call_args.kwargs["timeout"], 15.0)
        circuit.record_success.assert_called_once_with(
            "test|staging.example.test|alias:system",
            permit,
        )

    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_retries_circuit_outcome_after_state_write_failure(self, mock_request, mock_save_log):
        permit = object()

        class FlakyCircuit:
            def __init__(self):
                self.outcomes = []

            def admit(self, key):
                return permit

            def record_success(self, key, actual_permit):
                self.outcomes.append("success")
                raise BusinessRequestLimiterUnavailable("state unavailable")

            def record_failure(self, key, actual_permit):
                self.outcomes.append("failure")

        service = OperationalService()
        service.circuit_registry = FlakyCircuit()
        service._alert_failure = MagicMock()
        mock_request.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{"code":0}',
            json=lambda: {"code": 0},
        )

        with self.assertRaisesRegex(BusinessRequestLimiterUnavailable, "state unavailable"):
            service._call_api(
                "GET",
                "http://staging.example.test/admin-api/common/brands/public",
                "system",
                "refresh_configs",
            )

        self.assertEqual(service.circuit_registry.outcomes, ["success", "failure"])

    @patch('src.integrations.operational_service.save_operation_log')
    def test_call_api_test_prod_write_preserves_guard_error_without_circuit_outcome(self, mock_save_log):
        service = OperationalService()
        service.circuit_registry = MagicMock()

        with self.assertRaisesRegex(RuntimeError, "ENV=test 禁止向生产地址执行写操作"):
            service._call_api(
                "POST",
                "http://search.example.test/api/search/redis/setOpsLowWeight",
                "atlas",
                "dress",
                json={"keyword": "dress"},
            )

        service.circuit_registry.admit.assert_not_called()
        service.circuit_registry.record_failure.assert_not_called()

    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_injects_stable_idempotency_headers(self, mock_request, mock_save_log):
        service = OperationalService()
        mock_request.return_value = MagicMock(status_code=200, text='{"code":0}', json=lambda: {"code": 0})

        service._call_api(
            "POST",
            "http://staging.example.test/api/demo",
            "atlas",
            "q",
            json={"a": 1},
            headers={"Authorization": "Bearer test"},
            external_request_id="request-1",
        )

        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers["Idempotency-Key"], "request-1")
        self.assertEqual(headers["X-Request-ID"], "request-1")
        self.assertEqual(headers["Authorization"], "Bearer test")

    def test_get_top_query_action_status_confirms_query_demotion_from_readback(self):
        service = OperationalService()
        service.get_current_ops_low_weight_skus = MagicMock(return_value=["SKU-1", "SKU-2"])

        completed = service.get_top_query_action_status(
            action_type="query_demote",
            external_request_id="request-1",
            payload={"brand": "atlas", "query": "连衣裙", "product_ids": ["SKU-1"]},
        )
        missing = service.get_top_query_action_status(
            action_type="query_demote",
            external_request_id="request-1",
            payload={"brand": "atlas", "query": "连衣裙", "product_ids": ["SKU-3"]},
        )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(missing["status"], "not_found")

    def test_get_top_query_action_status_confirms_tag_bucket_config_from_readback(self):
        service = OperationalService()
        service.get_tag_bucket_config = MagicMock(return_value={
            "status": "available",
            "matched": True,
            "config": {"configs": [{"tags": "无袖", "afterBucket": "top"}]},
        })

        result = service.get_top_query_action_status(
            action_type="tag_bucket_config",
            external_request_id="request-1",
            payload={
                "brand": "atlas",
                "keyword": "连衣裙",
                "filters": [],
                "configs": [{"tags": "无袖", "afterBucket": "top"}],
            },
        )

        self.assertEqual(result["status"], "completed")

    def test_get_top_query_action_status_confirms_product_blacklist_from_query_list(self):
        service = OperationalService()
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {"list": [{"taggingUid": "SKU-1"}, {"taggingUid": "SKU-2"}], "total": 2},
        }
        service._call_api = MagicMock(return_value=response)

        result = service.get_top_query_action_status(
            action_type="product_blacklist",
            external_request_id="request-1",
            payload={
                "brand": "harbor",
                "query": "碎花连衣裙",
                "product_ids": ["SKU-1", "SKU-2"],
            },
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(service._call_api.call_args.kwargs["json"], {
            "blackList": "碎花连衣裙",
            "pageNo": 1,
            "pageSize": 100,
        })

    def test_get_top_query_action_status_marks_missing_product_blacklist_not_found(self):
        service = OperationalService()
        response = MagicMock(status_code=200)
        response.json.return_value = {"code": 0, "data": {"list": [], "total": 0}}
        service._call_api = MagicMock(return_value=response)

        result = service.get_top_query_action_status(
            action_type="product_blacklist",
            external_request_id="request-1",
            payload={
                "brand": "harbor",
                "query": "碎花连衣裙",
                "product_ids": ["SKU-1", "SKU-2"],
            },
        )

        self.assertEqual(result["status"], "not_found")

    def test_get_top_query_action_status_keeps_partial_product_blacklist_unknown(self):
        service = OperationalService()
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {"list": [{"taggingUid": "SKU-1"}], "total": 1},
        }
        service._call_api = MagicMock(return_value=response)

        result = service.get_top_query_action_status(
            action_type="product_blacklist",
            external_request_id="request-1",
            payload={
                "brand": "harbor",
                "query": "碎花连衣裙",
                "product_ids": ["SKU-1", "SKU-2"],
            },
        )

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "partial_product_blacklist_readback")
        self.assertEqual(result["response"]["unmatched_product_ids"], ["SKU-2"])

    def test_get_top_query_action_status_keeps_unsupported_writes_unknown(self):
        result = OperationalService().get_top_query_action_status(
            action_type="agent_top_products_replace",
            external_request_id="request-1",
            payload={"brand": "atlas", "query": "连衣裙", "product_ids": ["SKU-1"]},
        )

        self.assertEqual(result["status"], "unknown")

    def test_sliding_window_rate_limiter_sleeps_on_11th_request_within_one_second(self):
        current_time = [100.0]
        sleep_calls = []

        def fake_time():
            return current_time[0]

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            current_time[0] += seconds

        limiter = src.integrations.operational_service._SlidingWindowRateLimiter(
            max_requests=10,
            window_seconds=1.0,
            time_func=fake_time,
            sleep_func=fake_sleep,
        )

        for _ in range(10):
            limiter.acquire("POST /api/search/mixed")

        limiter.acquire("POST /api/search/mixed")

        self.assertEqual(len(sleep_calls), 1)
        self.assertAlmostEqual(sleep_calls[0], 1.0, places=4)

    @patch("src.integrations.operational_service.OperationalService._acquire_rate_limit_slot")
    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_does_not_use_legacy_endpoint_whitelist_limiter(self, mock_request, mock_save_log, mock_rate_limit):
        service = OperationalService()
        mock_request.return_value = MagicMock(status_code=200, text='{"code":0}', json=lambda: {"code": 0})

        service._call_api("POST", "http://staging.example.test/api/search/mixed", "atlas", "q", json={"a": 1})
        service._call_api("POST", "http://staging.example.test/admin-api/search/product/list", "atlas", "q", json={"a": 1})
        service._call_api("POST", "http://staging.example.test/api/demo", "atlas", "q", json={"a": 1})

        mock_rate_limit.assert_not_called()

    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_limits_every_business_endpoint_with_shared_identity(
        self,
        mock_request,
        mock_save_log,
    ):
        acquired_keys = []

        class RecordingGovernor:
            @contextmanager
            def acquire(self, key, request_timeout):
                acquired_keys.append((key, request_timeout))
                yield

            def request_with_rate_limit_retry(
                self, *, key, method, url, request_func, request_timeout, **kwargs
            ):
                kwargs.pop("max_attempts", None)
                with self.acquire(key, request_timeout):
                    return request_func(method, url, **kwargs)

        service = OperationalService()
        service.request_governor = RecordingGovernor()
        mock_request.return_value = MagicMock(
            status_code=200,
            text='{"code":0}',
            json=lambda: {"code": 0},
        )

        service._call_api(
            "POST",
            "http://staging.example.test/api/search/mixed",
            "atlas",
            "dress",
            json={"brand": "atlas", "keyword": "dress"},
            timeout=15,
        )
        service._call_api(
            "POST",
            "http://staging.example.test/admin-api/search/product/list",
            "atlas",
            "SKU-1",
            json={"spu": "SKU-1"},
            timeout=10,
        )
        service._call_api(
            "POST",
            "http://staging.example.test/api/search/redis/setOpsLowWeight",
            "atlas",
            "dress",
            json={"keyword": "dress"},
            timeout=10,
        )

        self.assertEqual(len(acquired_keys), 3)
        self.assertEqual(
            {key for key, _ in acquired_keys},
            {"test|staging.example.test|tenant:235"},
        )

    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_retries_429_after_retry_after_without_holding_lease(
        self,
        mock_request,
        mock_save_log,
    ):
        clock = FakeClock()
        service = OperationalService()
        service.request_governor = BusinessRequestGovernor(
            clock=clock.time,
            sleep=clock.sleep,
        )
        mock_request.side_effect = [
            MagicMock(status_code=429, headers={"Retry-After": "2"}, text="limited"),
            MagicMock(
                status_code=200,
                headers={},
                text='{"code":0}',
                json=lambda: {"code": 0},
            ),
        ]

        response = service._call_api(
            "POST",
            "http://staging.example.test/api/search/mixed",
            "atlas",
            "dress",
            json={"brand": "atlas", "keyword": "dress"},
            timeout=15,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(clock.sleeps, [2.0])

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_records_one_availability_failure_per_http_attempt(
        self,
        mock_request,
        mock_save_log,
        mock_notify,
    ):
        service = OperationalService()
        service.circuit_registry = CircuitRegistry(failure_threshold=2, cooldown_seconds=60)
        mock_request.return_value = MagicMock(
            status_code=503,
            headers={},
            text="unavailable",
        )

        service._call_api(
            "POST",
            "http://staging.example.test/api/search/mixed",
            "atlas",
            "dress",
            json={"brand": "atlas", "keyword": "dress"},
            timeout=15,
        )

        status = service.circuit_registry.status(
            "test|staging.example.test|tenant:235"
        )
        self.assertEqual(status["consecutive_failures"], 1)
        self.assertEqual(status["state"], "closed")

    def test_services_have_no_business_requests_outside_governed_exits(self):
        tree = ast.parse(Path(src.integrations.operational_service.__file__).read_text(encoding="utf-8"))
        direct_request_functions = set()

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
                    continue
                owner = child.func.value
                if (
                    isinstance(owner, ast.Name)
                    and owner.id == "requests"
                    and child.func.attr in {"get", "post", "request"}
                ):
                    direct_request_functions.add(node.name)

        self.assertEqual(
            direct_request_functions,
            {"get_token"},
        )

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_business_error_does_not_open_availability_circuit(self, mock_request, mock_save_log, mock_notify):
        service = OperationalService()
        mock_request.return_value = MagicMock(status_code=200, text='{"code":500,"msg":"bad"}', json=lambda: {"code": 500, "msg": "bad"})

        service._call_api("POST", "http://staging.example.test/api/demo", "atlas", "q", json={"a": 1})

        status = service.circuit_registry.status("test|staging.example.test|tenant:235")
        self.assertEqual(status["consecutive_failures"], 0)
        mock_notify.assert_called()

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.persistence.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_invalid_boolean_code_records_availability_failure_without_validator(
        self,
        mock_request,
        mock_save_log,
        mock_notify,
    ):
        service = OperationalService()
        service.circuit_registry = CircuitRegistry(
            failure_threshold=2,
            cooldown_seconds=60,
        )
        response = MagicMock(
            status_code=200,
            headers={},
            text='{"code":true}',
            json=lambda: {"code": True},
        )
        mock_request.return_value = response

        result = service._call_api(
            "POST",
            "http://staging.example.test/api/demo",
            "atlas",
            "q",
            json={"a": 1},
        )

        status = service.circuit_registry.status(
            "test|staging.example.test|tenant:235"
        )
        self.assertIs(result, response)
        self.assertEqual(status["consecutive_failures"], 1)
        self.assertEqual(status["state"], "closed")
        self.assertEqual(mock_save_log.call_args.args[0]["status"], "failed")
        self.assertIn("契约", mock_notify.call_args.args[0])

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.persistence.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_missing_code_keeps_compatibility_success(
        self,
        mock_request,
        mock_save_log,
        mock_notify,
    ):
        service = OperationalService()
        mock_request.return_value = MagicMock(
            status_code=200,
            headers={},
            text='{}',
            json=lambda: {},
        )

        service._call_api(
            "POST",
            "http://staging.example.test/api/demo",
            "atlas",
            "q",
            json={"a": 1},
        )

        status = service.circuit_registry.status(
            "test|staging.example.test|tenant:235"
        )
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertEqual(mock_save_log.call_args.args[0]["status"], "success")
        mock_notify.assert_not_called()

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_non_json_response_records_availability_failure(self, mock_request, mock_save_log, mock_notify):
        service = OperationalService()
        bad_resp = MagicMock(status_code=200, text="not-json")
        bad_resp.json.side_effect = ValueError("not json")
        mock_request.return_value = bad_resp

        service._call_api("POST", "http://staging.example.test/api/demo", "atlas", "q", json={"a": 1})

        status = service.circuit_registry.status("test|staging.example.test|tenant:235")
        self.assertEqual(status["consecutive_failures"], 1)
        mock_notify.assert_called()

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.persistence.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_plain_text_set_ops_ok_records_success(self, mock_request, mock_save_log, mock_notify):
        service = OperationalService()
        ok_resp = MagicMock(status_code=200, text="ok, total=6")
        ok_resp.json.side_effect = ValueError("not json")
        mock_request.return_value = ok_resp

        service._call_api(
            "POST",
            "http://staging.example.test/api/search/redis/setOpsLowWeight",
            "harbor",
            "batch_sync_redis",
            json={"vbnmenzb": {"T恤": ["1246290025"]}},
        )

        log_data = mock_save_log.call_args.args[0]
        self.assertEqual(log_data["status"], "success")
        mock_notify.assert_not_called()

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_network_exception_records_availability_failure(self, mock_request, mock_save_log, mock_notify):
        service = OperationalService()
        mock_request.side_effect = requests.RequestException("network down")

        with self.assertRaises(requests.RequestException):
            service._call_api("GET", "http://staging.example.test/api/demo", "atlas", "q")

        status = service.circuit_registry.status("test|staging.example.test|tenant:235")
        self.assertEqual(status["consecutive_failures"], 1)
        mock_notify.assert_called()

    def test_get_refined_query_strips_brand_tag(self):
        service = OperationalService()
        result = service.get_refined_query("atlas", "连衣裙 女士")
        self.assertEqual(result, "连衣裙")
        self.assertEqual(service.get_refined_query("atlas", "连衣裙"), "连衣裙")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.save_operation_log')
    def test_remove_from_blacklist_applies_tag_parsing(self, mock_save_log, mock_request):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        mock_request.return_value = MagicMock(status_code=200, json=lambda: {"code": 0}, text='{"code":0}')

        ok = service.remove_from_blacklist("atlas", "SPU1", ["裙子 女士"])

        self.assertTrue(ok)
        payload = mock_request.call_args.kwargs["json"]
        self.assertEqual(payload["blackList"], ["裙子"])
        self.assertEqual(payload["channelId"], "default")

    def test_remove_from_blacklist_summit_branch(self):
        service = OperationalService()
        with patch.object(service.summit_service, "update_blacklist", side_effect=[True, False]):
            ok = service.remove_from_blacklist("summit", "A;B", ["词"])
        self.assertFalse(ok)

    def test_ops_low_weight_capabilities_report_client_preflight_without_server_atomic_compare(self):
        service = OperationalService()

        self.assertFalse(service.supports_ops_low_weight_atomic_compare())
        self.assertTrue(service.supports_ops_low_weight_client_preflight())

    def test_sync_broken_code_to_redis_for_atlas_cleans_query_suffix(self):
        service = OperationalService()
        snapshots = [
            {"success": True, "product_ids": [], "version": "new-v1"},
            {"success": True, "product_ids": ["SKU1"], "version": "new-v2"},
        ]

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "get_ops_low_weight_config", side_effect=snapshots), \
             patch.object(service, "set_ops_low_weight_config", return_value={
                 "success": True,
                 "conflict": False,
                 "version": "new-v2",
             }) as mock_write:
            ok = service.sync_broken_code_to_redis(
                "atlas",
                add_items={"新词 男士": ["SKU1"]},
            )

        self.assertTrue(ok)
        self.assertEqual(mock_write.call_args.args[1:3], ("新词", ["SKU1"]))

    def test_sync_broken_code_to_redis_merges_with_current_ops_low_weight_snapshot(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        before = {"success": True, "product_ids": ["SKU_OLD"], "version": "v42"}
        after = {"success": True, "product_ids": ["SKU_NEW", "SKU_OLD"], "version": "v43"}

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "get_ops_low_weight_config", side_effect=[before, after]), \
             patch.object(service, "set_ops_low_weight_config", return_value={
                 "success": True,
                 "conflict": False,
                 "version": "v43",
             }) as mock_write:
            ok = service.sync_broken_code_to_redis("atlas", add_items={"皮衣": ["SKU_NEW"]})

        self.assertTrue(ok)
        self.assertEqual(mock_write.call_args.args[2], ["SKU_NEW", "SKU_OLD"])

    def test_sync_broken_code_to_redis_removes_only_target_skus_from_current_snapshot(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        before = {"success": True, "product_ids": ["SKU_KEEP", "SKU_REMOVE"], "version": "v42"}
        after = {"success": True, "product_ids": ["SKU_KEEP"], "version": "v43"}

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "get_ops_low_weight_config", side_effect=[before, after]), \
             patch.object(service, "set_ops_low_weight_config", return_value={
                 "success": True,
                 "conflict": False,
                 "version": "v43",
             }) as mock_write:
            ok = service.sync_broken_code_to_redis("atlas", remove_items={"皮衣": ["SKU_REMOVE"]})

        self.assertTrue(ok)
        self.assertEqual(mock_write.call_args.args[2], ["SKU_KEEP"])

    def test_sync_broken_code_to_redis_fails_closed_without_server_atomic_compare(self):
        service = OperationalService()

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=False), \
             patch.object(service, "get_ops_low_weight_config") as mock_read, \
             patch.object(service, "set_ops_low_weight_config") as mock_write, \
             patch.object(service, "_call_api") as mock_legacy_write:
            ok = service.sync_broken_code_to_redis(
                "atlas",
                add_items={"皮衣": ["SKU_NEW"]},
            )

        self.assertFalse(ok)
        mock_read.assert_not_called()
        mock_write.assert_not_called()
        mock_legacy_write.assert_not_called()

    def test_sync_broken_code_to_redis_blocks_legacy_prod_entry_even_with_atomic_compare(self):
        service = OperationalService()
        service.env = "prod"

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "supports_ops_low_weight_client_preflight", return_value=True), \
             patch.object(service, "get_ops_low_weight_config") as mock_read, \
             patch.object(service, "set_ops_low_weight_config") as mock_write:
            ok = service.sync_broken_code_to_redis(
                "atlas",
                add_items={"皮衣": ["SKU_NEW"]},
            )

        self.assertFalse(ok)
        mock_read.assert_not_called()
        mock_write.assert_not_called()

    def test_sync_broken_code_to_redis_allows_prod_recovery_only_with_readback(self):
        service = OperationalService()
        service.env = "prod"
        before = {"success": True, "product_ids": ["SKU_KEEP", "SKU_REMOVE"], "version": "v42"}
        after = {"success": True, "product_ids": ["SKU_KEEP"], "version": "v43"}

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=False), \
             patch.object(service, "get_ops_low_weight_config", side_effect=[before, after]), \
             patch.object(service, "set_ops_low_weight_config", return_value={
                 "success": True,
                 "conflict": False,
                 "version": "v43",
             }) as mock_write:
            ok = service.sync_broken_code_to_redis(
                "atlas",
                remove_items={"皮衣": ["SKU_REMOVE"]},
                allow_prod_recovery=True,
            )

        self.assertTrue(ok)
        mock_write.assert_called_once_with(
            "atlas",
            "皮衣",
            ["SKU_KEEP"],
            expected_version="v42",
            external_request_id=None,
        )

    def test_sync_broken_code_to_redis_rejects_prod_recovery_additions(self):
        service = OperationalService()
        service.env = "prod"

        with patch.object(service, "get_ops_low_weight_config") as mock_read, \
             patch.object(service, "set_ops_low_weight_config") as mock_write:
            ok = service.sync_broken_code_to_redis(
                "atlas",
                add_items={"皮衣": ["SKU_NEW"]},
                allow_prod_recovery=True,
            )

        self.assertFalse(ok)
        mock_read.assert_not_called()
        mock_write.assert_not_called()

    def test_sync_broken_code_to_redis_uses_authoritative_versioned_write_and_readback(self):
        service = OperationalService()
        before = {
            "success": True,
            "product_ids": ["SKU_OLD"],
            "version": "v42",
            "error_reason": None,
        }
        after = {
            "success": True,
            "product_ids": ["SKU_NEW", "SKU_OLD"],
            "version": "v43",
            "error_reason": None,
        }

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "get_ops_low_weight_config", side_effect=[before, after]) as mock_read, \
             patch.object(service, "set_ops_low_weight_config", return_value={
                 "success": True,
                 "conflict": False,
                 "version": "v43",
                 "error_reason": None,
             }) as mock_write, \
             patch.object(service, "_call_api") as mock_legacy_write:
            ok = service.sync_broken_code_to_redis(
                "atlas",
                add_items={"皮衣": ["SKU_NEW"]},
                external_request_id="request-1",
            )

        self.assertTrue(ok)
        self.assertEqual(mock_read.call_args_list, [call("atlas", "皮衣"), call("atlas", "皮衣")])
        mock_write.assert_called_once_with(
            "atlas",
            "皮衣",
            ["SKU_NEW", "SKU_OLD"],
            expected_version="v42",
            external_request_id="request-1",
        )
        mock_legacy_write.assert_not_called()

    def test_sync_broken_code_to_redis_preserves_empty_query_array_on_last_sku_removal(self):
        service = OperationalService()
        before = {
            "success": True,
            "product_ids": ["SKU_REMOVE"],
            "version": "v42",
            "error_reason": None,
        }
        after = {
            "success": True,
            "product_ids": [],
            "version": "v43",
            "error_reason": None,
        }

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "get_ops_low_weight_config", side_effect=[before, after]), \
             patch.object(service, "set_ops_low_weight_config", return_value={
                 "success": True,
                 "conflict": False,
                 "version": "v43",
                 "error_reason": None,
             }) as mock_write:
            ok = service.sync_broken_code_to_redis(
                "atlas",
                remove_items={"皮衣": ["SKU_REMOVE"]},
            )

        self.assertTrue(ok)
        mock_write.assert_called_once_with(
            "atlas",
            "皮衣",
            [],
            expected_version="v42",
            external_request_id=None,
        )

    def test_sync_broken_code_to_redis_rejects_multi_query_partial_write_contract(self):
        service = OperationalService()

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "get_ops_low_weight_config") as mock_read, \
             patch.object(service, "set_ops_low_weight_config") as mock_write:
            ok = service.sync_broken_code_to_redis(
                "atlas",
                add_items={"皮衣": ["SKU-1"], "裙子": ["SKU-2"]},
            )

        self.assertFalse(ok)
        mock_read.assert_not_called()
        mock_write.assert_not_called()

    def test_sync_broken_code_to_redis_returns_false_on_authoritative_readback_mismatch(self):
        service = OperationalService()
        before = {"success": True, "product_ids": [], "version": "v42"}
        mismatched = {"success": True, "product_ids": [], "version": "v43"}

        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "get_ops_low_weight_config", side_effect=[before, mismatched]), \
             patch.object(service, "set_ops_low_weight_config", return_value={
                 "success": True,
                 "conflict": False,
                 "version": "v43",
             }):
            ok = service.sync_broken_code_to_redis(
                "atlas",
                add_items={"皮衣": ["SKU-1"]},
            )

        self.assertFalse(ok)

    def test_sync_broken_code_to_redis_skips_summit(self):
        service = OperationalService()
        self.assertTrue(service.sync_broken_code_to_redis("summit", add_items={"q": ["sku"]}))

    def test_apply_tag_bucket_config_adds_when_query_filter_combo_does_not_exist(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        payload = {
            "keyword": "连衣裙",
            "filters": [{"field": "gender", "value": "WOMAN"}],
            "configs": [{"tags": "无袖,轻薄", "afterBucket": "top"}],
        }
        page_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": [], "total": 0}}, text='{"code":0}')
        add_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": "ok", "msg": ""}, text='{"code":0}')

        with patch.object(service, "_call_api", side_effect=[page_resp, add_resp]) as mock_call:
            result = service.apply_tag_bucket_config("atlas", payload)

        self.assertEqual(result["code"], 0)
        self.assertEqual(mock_call.call_args_list[0].args[1], "http://staging.example.test/admin-api/config/query-tags/page")
        self.assertEqual(mock_call.call_args_list[0].kwargs["json"], {"keyword": "连衣裙", "pageNo": 1, "pageSize": 20})
        self.assertEqual(mock_call.call_args_list[1].args[1], "http://staging.example.test/admin-api/config/query-tags/add")
        self.assertEqual(mock_call.call_args_list[1].kwargs["json"], payload)

    def test_apply_tag_bucket_config_for_atlas_splits_gender_suffix_into_filter(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        payload = {
            "keyword": "连衣裙 女士",
            "filters": [],
            "configs": [{"tags": "无袖,轻薄", "afterBucket": "top"}],
        }
        page_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": [], "total": 0}}, text='{"code":0}')
        add_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": "ok", "msg": ""}, text='{"code":0}')

        with patch.object(service, "_call_api", side_effect=[page_resp, add_resp]) as mock_call:
            result = service.apply_tag_bucket_config("atlas", payload)

        self.assertEqual(result["code"], 0)
        self.assertEqual(mock_call.call_args_list[0].kwargs["json"], {"keyword": "连衣裙", "pageNo": 1, "pageSize": 20})
        self.assertEqual(mock_call.call_args_list[1].kwargs["json"], {
            "keyword": "连衣裙",
            "filters": [{"field": "gender", "value": "WOMAN"}],
            "configs": [{"tags": "无袖,轻薄", "afterBucket": "top"}],
        })

    def test_apply_tag_bucket_config_updates_when_query_filter_combo_exists(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        payload = {
            "keyword": "连衣裙",
            "filters": [{"field": "gender", "value": "WOMAN"}],
            "configs": [{"tags": "无袖,轻薄", "afterBucket": "top"}],
        }
        existing = {
            "keyword": "连衣裙",
            "filters": [{"field": "gender", "value": "WOMAN"}],
            "configs": [{"tags": "长袖", "afterBucket": "top"}],
        }
        page_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": [existing], "total": 1}}, text='{"code":0}')
        update_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": "ok", "msg": ""}, text='{"code":0}')

        with patch.object(service, "_call_api", side_effect=[page_resp, update_resp]) as mock_call:
            result = service.apply_tag_bucket_config("atlas", payload)

        self.assertEqual(result["code"], 0)
        self.assertEqual(mock_call.call_args_list[1].args[1], "http://staging.example.test/admin-api/config/query-tags/update")
        self.assertEqual(mock_call.call_args_list[1].kwargs["json"], {
            "keyword": "连衣裙",
            "filters": [{"field": "gender", "value": "WOMAN"}],
            "configs": [
                {"tags": "长袖", "afterBucket": "top"},
                {"tags": "无袖,轻薄", "afterBucket": "top"},
            ],
        })

    def test_apply_tag_bucket_config_update_deduplicates_existing_configs(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        payload = {
            "keyword": "连衣裙",
            "filters": [{"field": "gender", "value": "WOMAN"}],
            "configs": [
                {"tags": "无袖,轻薄", "afterBucket": "top"},
                {"tags": "短袖", "afterBucket": "top"},
            ],
        }
        existing = {
            "keyword": "连衣裙",
            "filters": [{"field": "gender", "value": "WOMAN"}],
            "configs": [{"tags": "无袖,轻薄", "afterBucket": "top"}],
        }
        page_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": [existing], "total": 1}}, text='{"code":0}')
        update_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": "ok", "msg": ""}, text='{"code":0}')

        with patch.object(service, "_call_api", side_effect=[page_resp, update_resp]) as mock_call:
            result = service.apply_tag_bucket_config("atlas", payload)

        self.assertEqual(result["code"], 0)
        self.assertEqual(mock_call.call_args_list[1].kwargs["json"]["configs"], [
            {"tags": "无袖,轻薄", "afterBucket": "top"},
            {"tags": "短袖", "afterBucket": "top"},
        ])

    def test_apply_tag_bucket_config_returns_failure_when_page_query_fails(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        payload = {
            "keyword": "连衣裙",
            "filters": [],
            "configs": [{"tags": "无袖", "afterBucket": "top"}],
        }
        page_resp = MagicMock(status_code=500, text="server error")

        with patch.object(service, "_call_api", return_value=page_resp) as mock_call:
            result = service.apply_tag_bucket_config("atlas", payload)

        self.assertEqual(result["code"], -1)
        self.assertIn("query-tags/page", result["message"])
        self.assertEqual(mock_call.call_count, 1)

    def test_apply_tag_bucket_config_does_not_treat_add_or_update_failure_as_success(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        payload = {
            "keyword": "连衣裙",
            "filters": [],
            "configs": [{"tags": "无袖", "afterBucket": "top"}],
        }
        page_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": [], "total": 0}}, text='{"code":0}')
        add_resp = MagicMock(status_code=200, json=lambda: {"code": 10000, "data": None, "msg": "搜索词和过滤条件的组合已存在"}, text='{"code":10000}')

        with patch.object(service, "_call_api", side_effect=[page_resp, add_resp]):
            result = service.apply_tag_bucket_config("atlas", payload)

        self.assertEqual(result["code"], -1)
        self.assertEqual(result["tool_response"]["code"], 10000)

    def test_get_tag_bucket_config_returns_structured_match_for_keyword_and_filters(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        filters = [{"field": "gender", "value": "WOMAN"}]
        existing = {
            "keyword": "连衣裙",
            "filters": filters,
            "configs": [{"tags": "无袖,轻薄", "afterBucket": "top"}],
        }
        page_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": [existing], "total": 1}}, text='{"code":0}')

        with patch.object(service, "_call_api", return_value=page_resp) as mock_call:
            result = service.get_tag_bucket_config("atlas", "连衣裙", filters)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["config"], existing)
        self.assertTrue(result["matched"])
        self.assertEqual(mock_call.call_args.kwargs["json"], {"keyword": "连衣裙", "pageNo": 1, "pageSize": 20})

    def test_get_tag_bucket_config_for_atlas_splits_gender_suffix_into_filter(self):
        service = OperationalService()
        OperationalService.brand_configs["alias_atlas"] = {"apiKey": "k", "tenantId": 1}
        existing = {
            "keyword": "连衣裙",
            "filters": [{"field": "gender", "value": "WOMAN"}],
            "configs": [{"tags": "无袖,轻薄", "afterBucket": "top"}],
        }
        page_resp = MagicMock(status_code=200, json=lambda: {"code": 0, "data": {"list": [existing], "total": 1}}, text='{"code":0}')

        with patch.object(service, "_call_api", return_value=page_resp) as mock_call:
            result = service.get_tag_bucket_config("atlas", "连衣裙 女士", [])

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["config"], existing)
        self.assertEqual(mock_call.call_args.kwargs["json"], {"keyword": "连衣裙", "pageNo": 1, "pageSize": 20})

    def test_get_tag_bucket_config_returns_unsupported_for_summit_without_calling_api(self):
        service = OperationalService()

        with patch.object(service, "_call_api") as mock_call:
            result = service.get_tag_bucket_config("summit", "自行车", [])

        self.assertEqual(result["status"], "unsupported")
        self.assertFalse(result["matched"])
        self.assertIn("unsupported", result["tool_response"]["message"])
        mock_call.assert_not_called()

    def test_apply_tag_bucket_config_returns_unsupported_for_summit_without_calling_api(self):
        service = OperationalService()

        with patch.object(service, "_call_api") as mock_call:
            result = service.apply_tag_bucket_config(
                "summit",
                {
                    "keyword": "自行车",
                    "filters": [],
                    "configs": [{"tags": "夏季", "afterBucket": "top"}],
                },
            )

        self.assertEqual(result["code"], -1)
        self.assertIn("unsupported", result["message"])
        mock_call.assert_not_called()

    def test_query_available_tags_returns_existing_and_missing_tags(self):
        service = OperationalService()
        fake_kg = MagicMock()
        fake_kg.execute.side_effect = [
            {"code": 1, "data": [{"tagName": "无袖", "viewTag": "无袖"}]},
            {"code": 1, "data": []},
        ]
        service.kg_client = fake_kg

        result = service.query_available_tags("atlas", ["无袖", "轻薄"])

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["existing_tags"], [{"name": "无袖", "normalized_name": "无袖", "available": True}])
        self.assertEqual(result["missing_tags"], [{"name": "轻薄", "normalized_name": "轻薄", "available": False}])
        self.assertEqual(fake_kg.execute.call_args_list[0].args[0]["actionType"], "query_tag")

    def test_query_available_tags_distinguishes_empty_candidates_from_kg_miss(self):
        service = OperationalService()
        fake_kg = MagicMock()
        service.kg_client = fake_kg

        empty_result = service.query_available_tags("atlas", ["", "  "])

        self.assertEqual(empty_result, {
            "status": "no_candidate_tags",
            "existing_tags": [],
            "missing_tags": [],
            "tool_response": [],
        })
        fake_kg.execute.assert_not_called()

        fake_kg.execute.return_value = {"code": 0, "data": []}
        missing_result = service.query_available_tags("atlas", ["轻薄"])

        self.assertEqual(missing_result["status"], "not_found")
        self.assertEqual(missing_result["existing_tags"], [])
        self.assertEqual(missing_result["missing_tags"], [{
            "name": "轻薄",
            "normalized_name": "轻薄",
            "available": False,
        }])
        fake_kg.execute.assert_called_once()

    @patch('src.persistence.get_active_demoted_items', return_value={})
    def test_sync_broken_code_to_redis_returns_false_when_response_not_ok(self, mock_active):
        service = OperationalService()
        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "get_ops_low_weight_config", return_value={
                 "success": True,
                 "product_ids": [],
                 "version": "v42",
             }), \
             patch.object(service, "set_ops_low_weight_config", return_value={
                 "success": False,
                 "conflict": False,
                 "version": None,
                 "error_reason": "failed",
             }) as mock_write:
            self.assertFalse(service.sync_broken_code_to_redis("harbor", add_items={"q": ["sku"]}))
        mock_write.assert_called_once()

    @patch('src.persistence.get_active_demoted_items', return_value={})
    def test_sync_broken_code_to_redis_returns_false_on_exception(self, mock_active):
        service = OperationalService()
        with patch.object(service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(service, "get_ops_low_weight_config", return_value={
                 "success": True,
                 "product_ids": [],
                 "version": "v42",
             }), \
             patch.object(service, "set_ops_low_weight_config", side_effect=RuntimeError("sync error")) as mock_write:
            self.assertFalse(service.sync_broken_code_to_redis("harbor", add_items={"q": ["sku"]}))
        mock_write.assert_called_once()

    @patch('src.runtime.notifier.send_feishu_notification')
    @patch('src.integrations.operational_service.save_operation_log')
    @patch('src.integrations.operational_service.requests.request')
    def test_call_api_detects_redis_error_without_opening_availability_circuit(self, mock_request, mock_save_log, mock_notify):
        service = OperationalService()
        mock_request.return_value = MagicMock(status_code=200, text="error: bad gateway", json=lambda: {})

        service._call_api("POST", "http://staging.example.test/api/search/redis/setOpsLowWeight", "atlas", "q", json={"a": 1})

        status = service.circuit_registry.status("test|staging.example.test|tenant:235")
        self.assertEqual(status["consecutive_failures"], 0)
        mock_notify.assert_called()

    def test_get_product_broken_rate_routes_by_brand(self):
        service = OperationalService()
        with patch.object(service, "get_atlas_broken_rate", return_value=0.4) as mock_atlas, \
             patch.object(service, "get_harbor_broken_rate", return_value=0.5) as mock_harbor, \
             patch.object(service, "get_riviera_broken_rate", return_value=0.6) as mock_rl:
            self.assertEqual(service.get_product_broken_rate("atlas", {"attr1": "A"}), 0.4)
            self.assertEqual(service.get_product_broken_rate("harbor", {"productId": "B", "attr1": '{"zh_cn":"HARBOR"}'}), 0.5)
            self.assertEqual(service.get_product_broken_rate("riviera", {"spuId": "C", "attr1": '{"forcedPdpUrl":"/pdp"}'}), 0.6)
            self.assertIsNone(service.get_product_broken_rate("meadow", {"productId": "D"}))
            mock_atlas.assert_called_once_with("A")
            mock_harbor.assert_called_once_with("B")
            mock_rl.assert_called_once_with("C")

    def test_find_product_broken_rate_matches_multiple_id_fields(self):
        service = OperationalService()
        with patch.object(service, "search_products", return_value=[{"spuId": "A1", "productId": "B2", "skuId": "C3", "attr1": "D4"}]), \
             patch.object(service, "get_product_broken_rate", return_value=0.33) as mock_get:
            self.assertEqual(service.find_product_broken_rate("atlas", "query", "C3"), 0.33)
            mock_get.assert_called_once()
        self.assertIsNone(service.find_product_broken_rate("meadow", "query", "C3"))

    def test_find_product_broken_rate_falls_back_to_direct_product_id(self):
        service = OperationalService()
        with patch.object(service, "search_products", return_value=[]), \
             patch.object(service, "get_product_broken_rate", return_value=1.0) as mock_get:
            self.assertEqual(service.find_product_broken_rate("riviera", "粉色", "MAPOHGS0J422708"), 1.0)
            mock_get.assert_called_once_with("riviera", {"productId": "MAPOHGS0J422708"})

    @patch('src.integrations.operational_service.requests.get')
    def test_get_broken_rate_failures_return_none(self, mock_get):
        service = OperationalService()
        mock_get.return_value = MagicMock(status_code=500)
        self.assertIsNone(service.get_atlas_broken_rate("P1"))
        mock_get.return_value = MagicMock(status_code=500)
        self.assertIsNone(service.get_harbor_broken_rate("A1"))

    @patch('src.integrations.operational_service.requests.get')
    def test_get_atlas_broken_rate_handles_empty_and_exception(self, mock_get):
        service = OperationalService()
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"detail": {"colors": []}})
        self.assertIsNone(service.get_atlas_broken_rate("P1"))
        mock_get.side_effect = RuntimeError("atlas error")
        self.assertIsNone(service.get_atlas_broken_rate("P2"))

    @patch('src.integrations.operational_service.requests.get')
    def test_get_harbor_broken_rate_handles_empty_and_blank_id(self, mock_get):
        service = OperationalService()
        self.assertIsNone(service.get_harbor_broken_rate(""))
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"data": {"variant": []}})
        self.assertIsNone(service.get_harbor_broken_rate("A1"))

    @patch('src.integrations.operational_service.requests.post')
    @patch('src.integrations.operational_service.requests.get')
    def test_get_riviera_broken_rate_failure_returns_none(self, mock_get, mock_post):
        service = OperationalService()
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {"data": {"list": [{"skuList": []}]}})
        self.assertIsNone(service.get_riviera_broken_rate("SPU1"))
        mock_get.return_value = MagicMock(status_code=500)
        self.assertIsNone(service.get_riviera_broken_rate("SPU2"))

    def test_get_product_details_summit_refines_models(self):
        service = OperationalService()
        with patch.object(service.summit_service, "get_product_details", return_value=[{
            "models": [{"modelCode": "M1", "inventory": 8}],
            "inventory": 6,
            "status": "on_sale"
        }]):
            result = service.get_product_details("summit", "M1")

        self.assertEqual(result[0]["productId"], "M1")
        self.assertEqual(result[0]["inventory"], 8)

    def test_get_raw_product_details_preserves_summit_semantic_fields(self):
        service = OperationalService()
        raw = [{
            "webLabel": "女士徒步夹克",
            "imageUrl": "https://img.example/jacket.jpg",
            "models": [{"modelCode": "M1", "inventory": 8}],
        }]
        with patch.object(
            service.summit_service,
            "get_product_details_with_status",
            return_value={"success": True, "products": raw, "error_reason": None},
        ):
            result = service.get_raw_product_details("summit", "M1")

        self.assertEqual(result, raw)
        self.assertEqual(result[0]["webLabel"], "女士徒步夹克")

    def test_get_raw_product_details_reuses_standard_brand_detail_lookup(self):
        service = OperationalService()
        expected = [{"spu": "S1", "productName": "白色连衣裙"}]
        with patch.object(service, "get_product_details", return_value=expected) as detail:
            result = service.get_raw_product_details("atlas", "S1")

        self.assertEqual(result, expected)
        detail.assert_called_once_with("atlas", "S1")

    def test_calculate_broken_rate_returns_none_for_invalid_rows(self):
        service = OperationalService()
        self.assertIsNone(service.calculate_broken_rate([{"availability": "in_stock"}]))

    def test_get_ops_low_weight_config_paginates_authoritative_ops_bucket_and_hashes_sorted_ids(self):
        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        service._call_api = MagicMock(side_effect=[
            MagicMock(
                status_code=200,
                json=lambda: {
                    "code": 200,
                    "data": {
                        "total": 3,
                        "rows": [
                            {"skuId": "SKU-B", "ext": {"bucket": bucket}},
                            {"skuId": "VISIBLE", "ext": {"bucket": "normal"}},
                        ],
                        "searchLinks": [{"bucket": bucket, "total": 2}],
                    },
                },
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "code": 200,
                    "data": {
                        "total": 3,
                        "rows": [{"skuId": "SKU-A", "ext": {"bucket": bucket}}],
                        "searchLinks": [{"bucket": bucket, "total": 2}],
                    },
                },
            ),
        ])

        result = service.get_ops_low_weight_config("atlas", "连衣裙 女士")

        canonical = json.dumps(
            {"query": "连衣裙", "productIds": ["SKU-A", "SKU-B"]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected_version = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], ["SKU-A", "SKU-B"])
        self.assertEqual(result["version"], expected_version)
        self.assertEqual(service._call_api.call_count, 2)
        self.assertEqual(service._call_api.call_args_list[1].kwargs["json"]["pageNum"], 2)
        self.assertTrue(all(
            call.kwargs["redact_response_log"] is True
            for call in service._call_api.call_args_list
        ))

    def test_get_ops_low_weight_config_fails_closed_on_total_drift(self):
        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        service._call_api = MagicMock(side_effect=[
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {
                    "total": 2,
                    "rows": [{"skuId": "SKU-A", "ext": {"bucket": bucket}}],
                    "searchLinks": [{"bucket": bucket, "total": 2}],
                },
            }),
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {
                    "total": 3,
                    "rows": [{"skuId": "SKU-B", "ext": {"bucket": bucket}}],
                    "searchLinks": [{"bucket": bucket, "total": 2}],
                },
            }),
        ])

        result = service.get_ops_low_weight_config("atlas", "裙")

        self.assertFalse(result["success"])
        self.assertIsNone(result["version"])
        self.assertEqual(result["error_reason"], "authoritative_config_incomplete")

    def test_get_ops_low_weight_config_accepts_early_pagination_exhaustion(self):
        """上游 total 含被过滤商品时翻页提前耗尽（rows 空）；枚举照常结束。"""

        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        search_links = [{"bucket": bucket, "total": 1}]
        service._call_api = MagicMock(side_effect=[
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {
                    "total": 10,
                    "rows": [
                        {"skuId": "SKU-DEMOTE", "ext": {"bucket": bucket}},
                        *[
                            {"skuId": f"SKU-NORMAL-{index}", "ext": {"bucket": "normal"}}
                            for index in range(8)
                        ],
                    ],
                    "searchLinks": search_links,
                },
            }),
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {"total": 10, "rows": [], "searchLinks": search_links},
            }),
        ])

        result = service.get_ops_low_weight_config("atlas", "裙")

        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], ["SKU-DEMOTE"])
        self.assertEqual(result["version"], service._ops_low_weight_config_version("裙", ["SKU-DEMOTE"]))

    def test_get_ops_low_weight_config_fails_closed_when_ops_product_hidden_by_exhaustion(self):
        """提前耗尽且降权桶元数据>0 但降权商品不在可见行中时，仍拒绝。"""

        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        search_links = [{"bucket": bucket, "total": 1}]
        service._call_api = MagicMock(side_effect=[
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {
                    "total": 10,
                    "rows": [
                        {"skuId": f"SKU-NORMAL-{index}", "ext": {"bucket": "normal"}}
                        for index in range(9)
                    ],
                    "searchLinks": search_links,
                },
            }),
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {"total": 10, "rows": [], "searchLinks": search_links},
            }),
        ])

        result = service.get_ops_low_weight_config("atlas", "裙")

        self.assertFalse(result["success"])
        self.assertIsNone(result["version"])
        self.assertEqual(result["error_reason"], "authoritative_config_incomplete")

    def test_get_ops_low_weight_config_uses_observed_rows_when_bucket_total_is_pre_dedup(self):
        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        observed_ids = [f"SKU-{index}" for index in range(6)]
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 6,
                    "rows": [
                        {"skuId": product_id, "ext": {"bucket": bucket}}
                        for product_id in observed_ids
                    ],
                    "searchLinks": [{
                        "bucket": bucket,
                        "total": 10,
                        "action": "运营降低权重商品",
                    }],
                },
            },
        ))

        result = service.get_ops_low_weight_config("riviera", "亚麻")

        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], observed_ids)

    def test_get_ops_low_weight_config_fails_closed_when_positive_bucket_has_no_observed_rows(self):
        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 1,
                    "rows": [{"skuId": "NORMAL", "ext": {"bucket": "normal"}}],
                    "searchLinks": [{
                        "bucket": bucket,
                        "total": 1,
                        "action": "运营降低权重商品",
                    }],
                },
            },
        ))

        result = service.get_ops_low_weight_config("riviera", "亚麻")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_reason"], "authoritative_config_incomplete")

    def test_get_ops_low_weight_config_scans_all_rows_after_metadata_total_is_reached(self):
        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        first_rows = [
            {"skuId": "SKU-A", "ext": {"bucket": bucket}},
            *[
                {"skuId": f"NORMAL-{index}", "ext": {"bucket": "normal"}}
                for index in range(499)
            ],
        ]
        search_links = [{
            "bucket": bucket,
            "total": 1,
            "action": "运营降低权重商品",
        }]
        service._call_api = MagicMock(side_effect=[
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {
                    "total": 501,
                    "rows": first_rows,
                    "searchLinks": search_links,
                },
            }),
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {
                    "total": 501,
                    "rows": [{"skuId": "SKU-B", "ext": {"bucket": bucket}}],
                    "searchLinks": search_links,
                },
            }),
        ])

        result = service.get_ops_low_weight_config("riviera", "亚麻")

        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], ["SKU-A", "SKU-B"])
        self.assertEqual(service._call_api.call_count, 2)

    def test_get_ops_low_weight_config_fails_closed_on_duplicate_page(self):
        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        repeated_rows = [{"skuId": "SKU-A", "ext": {"bucket": bucket}}]
        page = {
            "code": 200,
            "data": {
                "total": 2,
                "rows": repeated_rows,
                "searchLinks": [{"bucket": bucket, "total": 2}],
            },
        }
        service._call_api = MagicMock(side_effect=[
            MagicMock(status_code=200, json=lambda: page),
            MagicMock(status_code=200, json=lambda: page),
        ])

        result = service.get_ops_low_weight_config("atlas", "裙")

        self.assertFalse(result["success"])
        self.assertIsNone(result["version"])
        self.assertEqual(result["error_reason"], "authoritative_config_incomplete")

    def test_get_ops_low_weight_config_fails_closed_when_bucket_product_id_is_missing(self):
        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 1,
                    "rows": [{"ext": {"bucket": bucket}}],
                    "searchLinks": [{"bucket": bucket, "total": 1}],
                },
            },
        ))

        result = service.get_ops_low_weight_config("atlas", "裙")

        self.assertFalse(result["success"])
        self.assertIsNone(result["version"])
        self.assertEqual(result["error_reason"], "invalid_authoritative_config_response")

    def test_observe_query_product_buckets_reads_authoritative_pages_and_ranks_target(self):
        service = OperationalService()
        first_rows = [
            {"skuId": f"SKU-{index}", "ext": {"bucket": "normal"}}
            for index in range(500)
        ]
        service._call_api = MagicMock(side_effect=[
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {"total": 501, "rows": first_rows},
            }),
            MagicMock(status_code=200, json=lambda: {
                "code": 200,
                "data": {
                    "total": 501,
                    "rows": [{
                        "taggingUid": "SKU-TARGET",
                        "ext": {"bucket": "productIdsByOpsLowWeight"},
                    }],
                },
            }),
        ])

        result = service.observe_query_product_buckets("atlas", "裙", ["SKU-TARGET"])

        self.assertTrue(result["success"])
        self.assertTrue(result["result_complete"])
        self.assertEqual(result["checked_product_count"], 501)
        self.assertEqual(result["observations"]["SKU-TARGET"]["rank"], 501)
        self.assertEqual(
            result["observations"]["SKU-TARGET"]["bucket"],
            "productIdsByOpsLowWeight",
        )

    def test_set_ops_low_weight_config_sends_explicit_empty_array(self):
        service = OperationalService()
        before_version = "sha256:before"
        service.get_ops_low_weight_config = MagicMock(return_value={
            "success": True,
            "version": before_version,
            "product_ids": ["SKU-OLD"],
            "error_reason": None,
        })
        service._call_api = MagicMock(return_value=MagicMock(status_code=200, text="  OK, total=0  "))

        result = service.set_ops_low_weight_config(
            "atlas",
            "连衣裙 女士",
            [],
            expected_version=before_version,
        )

        self.assertTrue(result["success"])
        self.assertEqual(service._call_api.call_args.kwargs["json"], {"alias_atlas": {"连衣裙": []}})
        self.assertIs(service._call_api.call_args.kwargs["redact_response_log"], True)

    def test_set_ops_low_weight_config_returns_conflict_without_write_for_stale_version(self):
        service = OperationalService()
        service.get_ops_low_weight_config = MagicMock(return_value={
            "success": True,
            "version": "sha256:current",
            "product_ids": ["SKU-CURRENT"],
            "error_reason": None,
        })
        service._call_api = MagicMock()

        result = service.set_ops_low_weight_config(
            "atlas",
            "裙",
            ["SKU-NEW"],
            expected_version="sha256:stale",
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["conflict"])
        self.assertEqual(result["http_status"], 409)
        service._call_api.assert_not_called()

    def test_set_ops_low_weight_config_accepts_only_strict_plain_text_success(self):
        cases = [
            (200, "  OK, total=1  ", True),
            (201, "ok", False),
            (200, "done", False),
            (200, "ok, error: write rejected", False),
            (200, "ok failed", False),
            (200, "ok rejected", False),
            (200, "not ok", False),
        ]
        for status_code, response_text, expected_success in cases:
            with self.subTest(status_code=status_code, response_text=response_text):
                service = OperationalService()
                service.get_ops_low_weight_config = MagicMock(return_value={
                    "success": True,
                    "version": "sha256:before",
                    "product_ids": [],
                    "error_reason": None,
                })
                service._call_api = MagicMock(return_value=MagicMock(
                    status_code=status_code,
                    text=response_text,
                ))

                result = service.set_ops_low_weight_config(
                    "atlas",
                    "裙",
                    ["SKU-A"],
                    expected_version="sha256:before",
                )

                self.assertEqual(result["success"], expected_success)

    @patch("src.persistence.save_operation_log")
    @patch("src.integrations.operational_service.requests.request")
    def test_call_api_redacts_response_log_when_requested(self, mock_request, mock_save_operation_log):
        service = OperationalService()
        payload = {
            "code": 200,
            "data": {
                "rows": [{"skuId": "SKU-SENSITIVE"}],
                "callback": "https://sensitive.example/path?token=token-secret-value",
            },
        }
        response_text = json.dumps(payload, ensure_ascii=False)
        response = MagicMock(status_code=200, text=response_text, json=lambda: payload)
        mock_request.return_value = response

        returned = service._call_api(
            "POST",
            "http://staging.example.test/api/search/mixed",
            "atlas",
            "裙",
            json={"brand": "alias_atlas", "keyword": "裙"},
            redact_response_log=True,
        )

        self.assertIs(returned, response)
        self.assertEqual(returned.json(), payload)
        self.assertNotIn("redact_response_log", mock_request.call_args.kwargs)
        log_data = mock_save_operation_log.call_args.args[0]
        self.assertEqual(log_data["response"], "[REDACTED]")
        serialized_log = json.dumps(log_data, ensure_ascii=False)
        for sensitive_value in (
            "SKU-SENSITIVE",
            "token-secret-value",
            "sensitive.example",
        ):
            self.assertNotIn(sensitive_value, serialized_log)

    @patch("src.persistence.save_operation_log")
    @patch("src.integrations.operational_service.requests.request")
    def test_call_api_keeps_response_log_by_default(self, mock_request, mock_save_operation_log):
        service = OperationalService()
        payload = {"code": 200, "data": {"rows": [{"skuId": "SKU-VISIBLE"}]}}
        response_text = json.dumps(payload, ensure_ascii=False)
        mock_request.return_value = MagicMock(
            status_code=200,
            text=response_text,
            json=lambda: payload,
        )

        service._call_api(
            "POST",
            "http://staging.example.test/api/search/mixed",
            "atlas",
            "裙",
            json={"brand": "alias_atlas", "keyword": "裙"},
        )

        log_data = mock_save_operation_log.call_args.args[0]
        self.assertEqual(log_data["response"], response_text)

    @patch("src.runtime.notifier.send_feishu_notification")
    @patch("src.persistence.save_operation_log")
    @patch("src.integrations.operational_service.requests.request")
    def test_call_api_redacts_business_error_log_and_alert(
        self,
        mock_request,
        mock_save_operation_log,
        mock_notifier,
    ):
        service = OperationalService()
        sensitive_message = (
            "SKU-BUSINESS token=business-secret "
            "https://sensitive.example/business?token=url-secret"
        )
        payload = {"code": 500, "msg": sensitive_message}
        response = MagicMock(
            status_code=200,
            text=json.dumps(payload, ensure_ascii=False),
            json=lambda: payload,
        )
        mock_request.return_value = response

        returned = service._call_api(
            "POST",
            "http://staging.example.test/api/search/mixed",
            "atlas",
            "裙",
            json={"brand": "alias_atlas", "keyword": "裙"},
            redact_response_log=True,
        )

        self.assertIs(returned, response)
        log_data = mock_save_operation_log.call_args.args[0]
        self.assertEqual(log_data["response"], "[REDACTED]")
        self.assertEqual(log_data["error"], "Business Error: code=500; details=[REDACTED]")
        alert_text = mock_notifier.call_args.args[0]
        self.assertIn("业务代码异常(500): [REDACTED]", alert_text)
        observed_text = json.dumps(log_data, ensure_ascii=False) + alert_text
        for sensitive_value in (
            "SKU-BUSINESS",
            "business-secret",
            "sensitive.example",
            "url-secret",
        ):
            self.assertNotIn(sensitive_value, observed_text)

    @patch("src.runtime.notifier.send_feishu_notification")
    @patch("src.persistence.save_operation_log")
    @patch("src.integrations.operational_service.requests.request")
    def test_call_api_redacts_non_2xx_response_body_from_log_and_alert(
        self,
        mock_request,
        mock_save_operation_log,
        mock_notifier,
    ):
        service = OperationalService()
        sensitive_body = (
            "token=http-secret "
            "https://sensitive.example/http?token=url-secret"
        )
        response = MagicMock(status_code=503, text=sensitive_body)
        mock_request.return_value = response

        returned = service._call_api(
            "POST",
            "http://staging.example.test/api/search/mixed",
            "atlas",
            "裙",
            json={"brand": "alias_atlas", "keyword": "裙"},
            redact_response_log=True,
        )

        self.assertIs(returned, response)
        log_data = mock_save_operation_log.call_args.args[0]
        self.assertEqual(log_data["response"], "[REDACTED]")
        self.assertEqual(log_data["status"], "failed")
        alert_text = mock_notifier.call_args.args[0]
        self.assertIn("HTTP 503", alert_text)
        observed_text = json.dumps(log_data, ensure_ascii=False) + alert_text
        for sensitive_value in ("http-secret", "sensitive.example", "url-secret"):
            self.assertNotIn(sensitive_value, observed_text)

    @patch("src.runtime.notifier.send_feishu_notification")
    @patch("src.persistence.save_operation_log")
    @patch("src.integrations.operational_service.requests.request")
    def test_call_api_redacts_network_exception_log_and_alert_without_changing_exception(
        self,
        mock_request,
        mock_save_operation_log,
        mock_notifier,
    ):
        service = OperationalService()
        exception = requests.RequestException(
            "postgresql://user:password@db.example/ops?token=network-secret"
        )
        mock_request.side_effect = exception

        with self.assertRaises(requests.RequestException) as raised:
            service._call_api(
                "POST",
                "http://staging.example.test/api/search/mixed",
                "atlas",
                "裙",
                json={"brand": "alias_atlas", "keyword": "裙"},
                redact_response_log=True,
            )

        self.assertIs(raised.exception, exception)
        log_data = mock_save_operation_log.call_args.args[0]
        self.assertEqual(log_data["error"], "RequestException: [REDACTED]")
        alert_text = mock_notifier.call_args.args[0]
        self.assertIn("网络异常(RequestException): [REDACTED]", alert_text)
        observed_text = json.dumps(log_data, ensure_ascii=False) + alert_text
        for sensitive_value in (
            "user:password",
            "db.example",
            "network-secret",
        ):
            self.assertNotIn(sensitive_value, observed_text)

    @patch("src.runtime.notifier.send_feishu_notification")
    @patch("src.persistence.save_operation_log")
    @patch("src.integrations.operational_service.requests.request")
    def test_call_api_keeps_business_error_log_and_alert_by_default(
        self,
        mock_request,
        mock_save_operation_log,
        mock_notifier,
    ):
        service = OperationalService()
        payload = {"code": 500, "msg": "raw-default-error"}
        response_text = json.dumps(payload, ensure_ascii=False)
        mock_request.return_value = MagicMock(
            status_code=200,
            text=response_text,
            json=lambda: payload,
        )

        service._call_api(
            "POST",
            "http://staging.example.test/api/search/mixed",
            "atlas",
            "裙",
            json={"brand": "alias_atlas", "keyword": "裙"},
        )

        log_data = mock_save_operation_log.call_args.args[0]
        self.assertEqual(log_data["response"], response_text)
        self.assertEqual(log_data["error"], "Business Error: raw-default-error")
        self.assertIn("业务代码异常(500): raw-default-error", mock_notifier.call_args.args[0])

    def test_get_ops_low_weight_config_treats_missing_bucket_metadata_as_empty(self):
        """上游约定：搜索链路未经过运营降权桶时 searchLinks 不返回该桶，等价于空集合。"""

        service = OperationalService()
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 1,
                    "rows": [{"skuId": "SKU-NORMAL", "ext": {"bucket": "normal"}}],
                    "searchLinks": [{"bucket": "normal", "total": 1}],
                },
            },
        ))

        result = service.get_ops_low_weight_config("atlas", "裙")

        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], [])
        self.assertEqual(
            result["version"],
            service._ops_low_weight_config_version("裙", []),
        )

    def test_get_ops_low_weight_config_fails_closed_when_rows_contradict_missing_bucket(self):
        """searchLinks 缺降权桶但 rows 中实际出现降权桶商品时，仍视为响应不完整。"""

        service = OperationalService()
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 2,
                    "rows": [
                        {"skuId": "SKU-NORMAL", "ext": {"bucket": "normal"}},
                        {"skuId": "SKU-DEMOTE", "ext": {"bucket": "productIdsByOpsLowWeight"}},
                    ],
                    "searchLinks": [{"bucket": "normal", "total": 2}],
                },
            },
        ))

        result = service.get_ops_low_weight_config("atlas", "裙")

        self.assertFalse(result["success"])
        self.assertEqual(result["product_ids"], [])
        self.assertIsNone(result["version"])
        self.assertEqual(result["error_reason"], "authoritative_config_incomplete")

    def test_get_ops_low_weight_config_accepts_explicit_zero_authoritative_bucket(self):
        service = OperationalService()
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 1,
                    "rows": [{"skuId": "SKU-NORMAL", "ext": {"bucket": "normal"}}],
                    "searchLinks": [
                        {"bucket": "normal", "total": 1},
                        {"bucket": "productIdsByOpsLowWeight", "total": 0},
                    ],
                },
            },
        ))

        result = service.get_ops_low_weight_config("atlas", "裙")

        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], [])
        self.assertEqual(
            result["version"],
            service._ops_low_weight_config_version("裙", []),
        )

    def test_get_ops_low_weight_config_accepts_explicit_compat_bucket_when_ops_bucket_absent(self):
        service = OperationalService()
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 2,
                    "rows": [
                        {
                            "skuId": "SKU-LOW",
                            "ext": {"bucket": "productIdsByLowWeight"},
                        },
                        {"skuId": "SKU-NORMAL", "ext": {"bucket": "normal"}},
                    ],
                    "searchLinks": [
                        {"bucket": "normal", "total": 1},
                        {"bucket": "productIdsByLowWeight", "total": 1},
                    ],
                },
            },
        ))

        result = service.get_ops_low_weight_config("atlas", "中长裙 女士")

        self.assertTrue(result["success"])
        self.assertEqual(result["normalized_query"], "中长裙")
        self.assertEqual(result["product_ids"], ["SKU-LOW"])
        self.assertEqual(
            result["version"],
            service._ops_low_weight_config_version("中长裙", ["SKU-LOW"]),
        )

    def test_get_ops_low_weight_config_accepts_legacy_bucket_action_name(self):
        service = OperationalService()
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 2,
                    "rows": [
                        {
                            "skuId": "SKU-LEGACY-LOW",
                            "ext": {"bucket": "productIdsByLowWeight"},
                        },
                        {"skuId": "SKU-NORMAL", "ext": {"bucket": "normal"}},
                    ],
                    "searchLinks": [
                        {"bucket": "normal", "total": 1},
                        {
                            "bucket": "productIdsByLowWeight",
                            "total": 1,
                            "action": "需要降低权重商品",
                        },
                    ],
                },
            },
        ))

        result = service.get_ops_low_weight_config("atlas", "女士")

        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], ["SKU-LEGACY-LOW"])
        self.assertEqual(
            result["version"],
            service._ops_low_weight_config_version("女士", ["SKU-LEGACY-LOW"]),
        )

    def test_get_ops_low_weight_config_prefers_ops_bucket_when_both_contracts_are_present(self):
        service = OperationalService()
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 3,
                    "rows": [
                        {
                            "skuId": "SKU-OPS",
                            "ext": {"bucket": "productIdsByOpsLowWeight"},
                        },
                        {
                            "skuId": "SKU-COMPAT",
                            "ext": {"bucket": "productIdsByLowWeight"},
                        },
                        {"skuId": "SKU-NORMAL", "ext": {"bucket": "normal"}},
                    ],
                    "searchLinks": [
                        {"bucket": "normal", "total": 1},
                        {"bucket": "productIdsByLowWeight", "total": 1},
                        {"bucket": "productIdsByOpsLowWeight", "total": 1},
                    ],
                },
            },
        ))

        result = service.get_ops_low_weight_config("atlas", "裙")

        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], ["SKU-OPS"])

    def test_get_ops_low_weight_config_ignores_dynamic_tag_segments_for_authoritative_ops_bucket(self):
        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            json=lambda: {
                "code": 200,
                "data": {
                    "total": 4,
                    "rows": [
                        {
                            "skuId": "SKU-DYNAMIC",
                            "ext": {"bucket": bucket},
                        },
                        {
                            "skuId": "SKU-OPS",
                            "ext": {"bucket": bucket},
                        },
                        {"skuId": "SKU-NORMAL-1", "ext": {"bucket": "top"}},
                        {"skuId": "SKU-NORMAL-2", "ext": {"bucket": "top"}},
                    ],
                    "searchLinks": [
                        {
                            "bucket": "top",
                            "total": 2,
                        },
                        {
                            "bucket": bucket,
                            "total": 1,
                            "dynamic": True,
                            "action": "标签权重",
                            "key": "儿童",
                        },
                        {
                            "bucket": bucket,
                            "total": 1,
                            "dynamic": None,
                            "action": "运营降低权重商品",
                        },
                    ],
                },
            },
        ))

        result = service.get_ops_low_weight_config("riviera", "无袖")

        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], ["SKU-OPS"])
        self.assertEqual(
            result["version"],
            service._ops_low_weight_config_version("无袖", ["SKU-OPS"]),
        )

    def test_get_ops_low_weight_config_applies_authoritative_bucket_offset_across_pages(self):
        service = OperationalService()
        bucket = "productIdsByOpsLowWeight"
        search_links = [
            {
                "bucket": bucket,
                "total": 2,
                "dynamic": True,
                "action": "标签权重",
                "key": "儿童",
            },
            {
                "bucket": bucket,
                "total": 1,
                "dynamic": None,
                "action": "运营降低权重商品",
            },
        ]
        service._call_api = MagicMock(side_effect=[
            MagicMock(
                status_code=200,
                json=lambda: {
                    "code": 200,
                    "data": {
                        "total": 3,
                        "rows": [{"skuId": "SKU-DYNAMIC-1", "ext": {"bucket": bucket}}],
                        "searchLinks": search_links,
                    },
                },
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "code": 200,
                    "data": {
                        "total": 3,
                        "rows": [
                            {"skuId": "SKU-DYNAMIC-2", "ext": {"bucket": bucket}},
                            {"skuId": "SKU-OPS", "ext": {"bucket": bucket}},
                        ],
                        "searchLinks": search_links,
                    },
                },
            ),
        ])

        result = service.get_ops_low_weight_config("riviera", "无袖")

        self.assertTrue(result["success"])
        self.assertEqual(result["product_ids"], ["SKU-OPS"])
        self.assertEqual(service._call_api.call_count, 2)

    def _service_with_current_ops_version(self):
        service = OperationalService()
        service.get_ops_low_weight_config = MagicMock(return_value={
            "success": True,
            "version": "sha256:before",
            "product_ids": [],
            "error_reason": None,
        })
        return service

    def test_set_ops_low_weight_config_redacts_http_error_tool_response(self):
        service = self._service_with_current_ops_version()
        sensitive_text = (
            "rejected SKU-HTTP token=http-secret "
            "https://sensitive.example/http?token=url-secret"
        )
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=500,
            text=sensitive_text,
        ))

        result = service.set_ops_low_weight_config(
            "atlas",
            "裙",
            ["SKU-A"],
            expected_version="sha256:before",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_reason"], "conditional_write_http_error")
        self.assertEqual(result["http_status"], 500)
        self.assertEqual(result["tool_response"], {
            "code": 500,
            "message": "conditional_write_http_error: [REDACTED]",
        })
        result_text = json.dumps(result, ensure_ascii=False)
        for sensitive_value in ("SKU-HTTP", "http-secret", "sensitive.example", "url-secret"):
            self.assertNotIn(sensitive_value, result_text)

    def test_set_ops_low_weight_config_redacts_business_error_tool_response(self):
        service = self._service_with_current_ops_version()
        sensitive_text = json.dumps({
            "code": 500,
            "msg": (
                "rejected SKU-BUSINESS token=business-secret "
                "https://sensitive.example/business?token=url-secret"
            ),
        }, ensure_ascii=False)
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            text=sensitive_text,
        ))

        result = service.set_ops_low_weight_config(
            "atlas",
            "裙",
            ["SKU-A"],
            expected_version="sha256:before",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_reason"], "invalid_conditional_write_response")
        self.assertEqual(result["tool_response"], {
            "code": 200,
            "message": "invalid_conditional_write_response: [REDACTED]",
        })
        result_text = json.dumps(result, ensure_ascii=False)
        for sensitive_value in (
            "SKU-BUSINESS",
            "business-secret",
            "sensitive.example",
            "url-secret",
        ):
            self.assertNotIn(sensitive_value, result_text)

    def test_set_ops_low_weight_config_redacts_network_exception_tool_response(self):
        service = self._service_with_current_ops_version()
        service._call_api = MagicMock(side_effect=requests.RequestException(
            "rejected SKU-NETWORK "
            "postgresql://user:password@db.example/ops?token=network-secret"
        ))

        result = service.set_ops_low_weight_config(
            "atlas",
            "裙",
            ["SKU-A"],
            expected_version="sha256:before",
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["error_reason"], "conditional_write_request_failed")
        self.assertEqual(result["tool_response"], {
            "code": None,
            "message": "RequestException: [REDACTED]",
        })
        result_text = json.dumps(result, ensure_ascii=False)
        for sensitive_value in (
            "SKU-NETWORK",
            "user:password",
            "db.example",
            "network-secret",
        ):
            self.assertNotIn(sensitive_value, result_text)

    def test_set_ops_low_weight_config_redacts_success_tool_response_details(self):
        service = self._service_with_current_ops_version()
        sensitive_text = (
            "ok, updated SKU-SUCCESS token=success-secret "
            "https://sensitive.example/success?token=url-secret"
        )
        service._call_api = MagicMock(return_value=MagicMock(
            status_code=200,
            text=sensitive_text,
        ))

        result = service.set_ops_low_weight_config(
            "atlas",
            "裙",
            ["SKU-A"],
            expected_version="sha256:before",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["tool_response"], {
            "code": 200,
            "message": "conditional_write_succeeded: [REDACTED]",
        })
        result_text = json.dumps(result, ensure_ascii=False)
        for sensitive_value in (
            "SKU-SUCCESS",
            "success-secret",
            "sensitive.example",
            "url-secret",
        ):
            self.assertNotIn(sensitive_value, result_text)

if __name__ == '__main__':
    unittest.main()
