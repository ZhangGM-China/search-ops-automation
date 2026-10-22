import os
import unittest
from unittest.mock import patch, MagicMock
from src.llm.agents import (
    NoResultQueryIntentAgent,
    NoResultDiagnosisAgent,
)
from src.llm.node_config import NodeConfigError, NodeConfigSnapshot, ResolvedNodeConfig, capture_snapshot

class TestAgentsExtended(unittest.TestCase):

    @patch('src.llm.agents.build_rd_chat_model')
    def test_no_result_agents_share_the_central_rd_llm_builder(self, mock_builder):
        mock_builder.return_value = MagicMock()

        NoResultQueryIntentAgent()
        NoResultDiagnosisAgent()

        self.assertEqual(mock_builder.call_count, 2)
        self.assertTrue(all(call.kwargs["default_model"] == "qwen3.6-plus" for call in mock_builder.call_args_list))

    @patch('src.llm.agents.build_rd_chat_model')
    def test_no_result_agents_use_the_same_frozen_snapshot_models(self, mock_builder):
        mock_builder.return_value = MagicMock()
        snapshot = capture_snapshot(
            node_keys=("no_result.intent", "no_result.diagnosis"),
            published={
                "no_result.intent": {
                    "primaryModel": "DeepSeek-V3",
                    "fallbackModel": "DeepSeek-R1",
                },
                "no_result.diagnosis": {
                    "primaryModel": "DeepSeek-V3.1",
                    "fallbackModel": "DeepSeek-R1.1",
                },
            },
        )

        NoResultQueryIntentAgent(model_config_snapshot=snapshot)
        NoResultDiagnosisAgent(model_config_snapshot=snapshot)

        self.assertEqual(
            [call.kwargs["default_model"] for call in mock_builder.call_args_list],
            ["DeepSeek-V3", "DeepSeek-R1", "DeepSeek-V3.1", "DeepSeek-R1.1"],
        )
        self.assertEqual(mock_builder.call_args_list[2].kwargs["timeout"], 100.0)
        self.assertEqual(mock_builder.call_args_list[3].kwargs["timeout"], 100.0)

    def test_no_result_agents_reject_missing_or_non_rd_llm_snapshot(self):
        invalid_snapshots = [
            NodeConfigSnapshot(captured_at="", nodes={}),
            NodeConfigSnapshot(
                captured_at="",
                nodes={
                    "no_result.intent": ResolvedNodeConfig(
                        node_key="no_result.intent",
                        channel="local_llm",
                        primary_model="DeepSeek-V3",
                        fallback_model=None,
                        primary_source="published",
                        fallback_source="published",
                    ),
                    "no_result.diagnosis": ResolvedNodeConfig(
                        node_key="no_result.diagnosis",
                        channel="rd_llm",
                        primary_model="DeepSeek-V3",
                        fallback_model=None,
                        primary_source="published",
                        fallback_source="published",
                    ),
                },
            ),
        ]

        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(NodeConfigError):
                    NoResultQueryIntentAgent(model_config_snapshot=snapshot)

    @patch('src.llm.agents.build_rd_chat_model')
    @patch('src.persistence.save_operation_log')
    def test_no_result_query_intent_agent_filters_non_shopping_query(self, mock_save, mock_llm_class):
        mock_resp = MagicMock()
        mock_resp.content = '{"decision":"ignore","reason":"积分兑换非购物搜索","confidence":0.96}'
        mock_llm_class.return_value.invoke.return_value = mock_resp

        agent = NoResultQueryIntentAgent()
        result = agent.classify("积分兑换", "harbor")

        self.assertEqual(result["decision"], "ignore")
        self.assertIn("非购物", result["reason"])

    @patch('src.llm.agents.build_rd_chat_model')
    @patch('src.persistence.save_operation_log')
    def test_no_result_diagnosis_agent_returns_structured_plan(self, mock_save, mock_llm_class):
        mock_resp = MagicMock()
        mock_resp.content = """
        ```json
        {
          "outputType": "strategy",
          "reason": "缺少同义关系",
          "confidence": 0.87,
          "actions": [
            {
              "actionType": "add_relationship",
              "targetInterface": "tagRelationship/addRelationship",
              "riskLevel": "low",
              "reason": "粉红色应同义于粉色",
              "params": {"tagName": "粉红色", "responseTagName": "粉色"}
            }
          ]
        }
        ```
        """
        mock_llm_class.return_value.invoke.return_value = mock_resp

        agent = NoResultDiagnosisAgent()
        plan = agent.diagnose("粉红色大衣", ["粉红色", "大衣"], {"tags": []})

        self.assertEqual(plan["outputType"], "strategy")
        self.assertEqual(plan["actions"][0]["actionType"], "add_relationship")
        self.assertEqual(plan["actions"][0]["riskLevel"], "low")
        self.assertIn("_llm_metrics", plan)
        self.assertIsNone(plan["_llm_metrics"]["errorType"])

    @patch.dict(os.environ, {"NO_RESULT_AGENT_TIMEOUT_SECONDS": "17"})
    @patch('src.llm.agents.build_rd_chat_model')
    def test_no_result_diagnosis_agent_configures_request_timeout(self, mock_llm_class):
        NoResultDiagnosisAgent()

        self.assertEqual(mock_llm_class.call_args.kwargs["timeout"], 17.0)

if __name__ == '__main__':
    unittest.main()
