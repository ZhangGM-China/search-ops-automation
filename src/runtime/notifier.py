import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()

def send_feishu_notification(content: str, title: str = "自动化运维汇报"):
    """
    发送消息到飞书群机器人
    支持 Markdown 格式
    """
    if os.getenv("AIOPS_OFFLINE_TEST") == "1":
        return False

    webhook_url = os.getenv("FEISHU_WEBHOOK_URL")
    if not webhook_url:
        logging.error("未配置 FEISHU_WEBHOOK_URL")
        return False

    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [
                        [
                            {
                                "tag": "text",
                                "text": content
                            }
                        ]
                    ]
                }
            }
        }
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            logging.error(f"飞书消息发送失败: {resp.text}")
            return False
    except Exception as e:
        logging.error(f"飞书消息发送异常: {e}")
        return False

if __name__ == "__main__":
    # 测试发送
    test_content = "这是一条来自自动化运维工具的测试消息。\n当前状态：联调中\n环境：测试环境"
    send_feishu_notification(test_content, "【运维测试】连接验证")
