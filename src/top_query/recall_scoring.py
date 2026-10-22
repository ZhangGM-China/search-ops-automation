from typing import Any, Dict, List, Optional


RECALL_SCORE_VERSION = "v3_explainable"


def build_recall_assessment(
    *,
    result_count: int,
    low_result_threshold: int,
    drop_ratio_threshold: float,
    seven_day_average: Optional[float],
    history_sample_count: int,
) -> Dict[str, Any]:
    current_count = max(int(result_count or 0), 0)
    low_threshold = max(int(low_result_threshold or 0), 1)
    drop_threshold = max(float(drop_ratio_threshold or 0), 0.0)
    sample_count = max(int(history_sample_count or 0), 0)
    average_count = float(seven_day_average) if seven_day_average is not None else None

    if current_count == 0:
        supply_score = 0
        supply_rule = "当前无召回结果，绝对供给不得分"
    elif current_count < low_threshold:
        supply_score = 9
        supply_rule = f"当前召回低于少结果阈值{low_threshold}件，绝对供给得9分"
    else:
        supply_score = 18
        supply_rule = f"当前召回达到少结果阈值{low_threshold}件，绝对供给得满分"

    drop_ratio: Optional[float] = None
    if supply_score < 18:
        stability_score = 0
        stability_rule = "绝对供给不足，不计算历史稳定性得分"
    elif sample_count < 3 or average_count is None or average_count <= 0:
        stability_score = 12
        stability_rule = "历史样本不足3天，不判断结果数量骤降"
    else:
        drop_ratio = round(max((average_count - current_count) / average_count, 0.0), 4)
        if drop_ratio <= drop_threshold:
            stability_score = 12
            stability_rule = f"下降比例未超过{drop_threshold:.0%}阈值，历史稳定性得满分"
        elif drop_ratio <= drop_threshold + 0.2:
            stability_score = 8
            stability_rule = f"下降比例超过{drop_threshold:.0%}阈值不超过20个百分点，扣4分"
        elif drop_ratio <= drop_threshold + 0.4:
            stability_score = 4
            stability_rule = f"下降比例超过{drop_threshold:.0%}阈值20至40个百分点，扣8分"
        else:
            stability_score = 0
            stability_rule = f"下降比例超过{drop_threshold:.0%}阈值40个百分点以上，扣12分"

    reasons: List[str] = []
    if current_count == 0:
        reasons.append("no_result")
    elif current_count < low_threshold:
        reasons.append("low_result")
    if drop_ratio is not None and drop_ratio > drop_threshold:
        reasons.append("result_count_drop")

    final_score = supply_score + stability_score
    status = "needs_attention" if reasons else "healthy"
    if current_count >= low_threshold and "result_count_drop" in reasons:
        summary = f"召回{current_count}件，数量充足，但较近7日均值下降{drop_ratio:.1%}"
    elif current_count >= low_threshold:
        summary = f"召回{current_count}件，数量充足"
    elif current_count == 0:
        summary = "当前无召回商品"
    else:
        summary = f"召回{current_count}件，低于{low_threshold}件阈值"

    rationale = summary
    if sample_count < 3 and current_count >= low_threshold:
        rationale = f"{summary}；历史样本不足3天，本次不做骤降扣分"

    trace = {
        "version": RECALL_SCORE_VERSION,
        "source": "rule",
        "rationale": rationale,
        "finalScore": final_score,
        "maxScore": 30,
        "items": [
            {
                "key": "absolute_supply",
                "label": "绝对供给",
                "score": supply_score,
                "maxScore": 18,
                "deduction": 18 - supply_score,
                "rule": supply_rule,
                "evidence": [
                    {"label": "当前召回商品数", "value": current_count},
                    {"label": "少结果阈值", "value": low_threshold},
                ],
            },
            {
                "key": "historical_stability",
                "label": "历史稳定性",
                "score": stability_score,
                "maxScore": 12,
                "deduction": 12 - stability_score,
                "rule": stability_rule,
                "evidence": [
                    {"label": "近7日平均召回数", "value": average_count if average_count is not None else "暂无"},
                    {"label": "历史样本数", "value": sample_count},
                    {"label": "下降比例", "value": f"{drop_ratio:.1%}" if drop_ratio is not None else "未计算"},
                    {"label": "骤降阈值", "value": f"{drop_threshold:.0%}"},
                ],
            },
        ],
    }
    return {
        "score": final_score,
        "status": status,
        "summary": summary,
        "rationale": rationale,
        "reasons": reasons,
        "drop_ratio": drop_ratio,
        "scoring_trace": trace,
    }
