import sys
import os
from pathlib import Path
import unittest
from unittest.mock import patch

# 加载项目路径
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from src.integrations.operational_service import SummitService
from src.persistence import reset_engine_for_testing, Base

class TestSummitLogic(unittest.TestCase):
    def setUp(self):
        self.engine = reset_engine_for_testing()
        Base.metadata.create_all(bind=self.engine)
        # Reset shared token to ensure isolation
        SummitService._shared_token = None
        env_patch = patch.dict(
            os.environ,
            {"SUMMIT_OAUTH_TOKEN": "test-oauth-basic"},
            clear=False,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.service = SummitService(env="test")

    @patch('src.integrations.operational_service.requests.request')
    @patch('src.integrations.operational_service.requests.post')
    def test_search_and_prompt_logic(self, mock_post, mock_request):
        # 1. Mock Token Response
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"access_token": "fake_token", "expires_in": 3600}
        
        # 2. Mock Search Response
        mock_request.return_value.status_code = 200
        mock_request.return_value.json.return_value = {
            "datas": {
                "dsmList": [
                    {
                        "dsmCode": "DSM123",
                        "productNatureText": "SPU类别",
                        "family": "SPU系列",
                        "designedFor": "SPU适用",
                        "models": [
                            {
                                "modelCode": "MC001",
                                "webLabel": "款式1名称",
                                "tagName": "款式1标签",
                                "price": {"activePrice": 100},
                                "media": {"images": ["img1"]}
                            },
                            {
                                "modelCode": "MC002",
                                "webLabel": "款式2名称",
                                "tagName": "款式2标签",
                                "price": {"activePrice": 200},
                                "media": {"images": ["img2"]}
                            }
                        ]
                    }
                ]
            }
        }
        
        # 执行搜索
        rows = self.service.search_products("test query")
        self.assertEqual(len(rows), 1)
        product = rows[0]
        
        # 验证 ID 拼接 (应该是 modelCode 拼接)
        self.assertEqual(product["productId"], "MC001;MC002")
        self.assertEqual(product["spuId"], "MC001;MC002")
        
        # 验证款式明细聚合
        models_details = product.get("summit_models_details", [])
        self.assertEqual(len(models_details), 2)
        self.assertEqual(models_details[0]["modelCode"], "MC001")
        self.assertEqual(models_details[1]["modelCode"], "MC002")
        
        # 验证 SPU 级信息是否合并到了款式明细中
        self.assertEqual(models_details[0]["mainCategory"], "SPU类别")
        
        # 验证 Agent Prompt 组装
        # 我们模拟 Agent.check 内部逻辑
        user_content = ""
        is_summit = product.get("brand") == "summit"
        if is_summit and models_details:
            user_content = f"用户搜索词 (Query): test\n"
            for i, d in enumerate(models_details):
                web_label = d.get("webLabel") or ""
                user_content += f"\n款式{i+1}的商品信息:\n- 商品名称（webLabel）: {web_label}\n"
        
        print("\nGenerated User Content:")
        print(user_content)
        self.assertIn("款式1的商品信息:", user_content)
        self.assertIn("款式2的商品信息:", user_content)
        self.assertIn("款式1名称", user_content)
        self.assertIn("款式2名称", user_content)

        # 3. 验证拉黑接口 Payload
        # 模拟 OperationalService.add_to_blacklist 行为
        pids = product["productId"].split(";")
        for pid in pids:
            # 模拟 SummitService.update_blacklist 内部构造
            # 注意: update_blacklist 必须先确认详情行与目标商品一致
            with patch.object(
                self.service,
                'get_product_details_with_status',
                return_value={
                    "success": True,
                    "products": [{"productId": pid, "blackListAi": ""}],
                    "error_reason": None,
                },
            ):
                # 重新 mock request 以捕获拉黑请求
                mock_request.reset_mock()
                mock_request.return_value.status_code = 200
                mock_request.return_value.json.return_value = {"code": 0}
                
                self.assertTrue(self.service.update_blacklist(pid, ["word1"]))
                
                # 获取最后一次请求的 payload
                args, kwargs = mock_request.call_args
                payload = kwargs.get("json")
                print(f"Blacklist Payload for {pid}: {payload}")
                
                self.assertEqual(payload["productId"], f"summit_{pid}")

if __name__ == "__main__":
    unittest.main()
