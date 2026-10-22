from datetime import date

from src.query_governance.visibility import (
    CHECK_KEYS,
    effective_check_modes,
    visible_check_keys,
    check_modes_for_date,
    record_check_mode_changes,
)


def test_effective_check_modes_merges_brand_switches_without_ui_version_fields():
    modes = effective_check_modes({
        "query_governance_reorder_enabled": False,
        "query_governance_relevance_enabled": False,
        "top_query_check_modes": {
            "recall": "disabled",
            "relevance": "auto",
            "open_factor": "disabled",
            "inventory": "disabled",
        },
    })
    assert modes == {
        "recall": False,
        "open_factor": False,
        "relevance": False,
        "inventory": False,
    }
    assert tuple(modes) == CHECK_KEYS


def test_visible_check_keys_keep_historical_facts_when_current_switch_is_off():
    assert visible_check_keys(
        {"recall": False, "open_factor": False, "relevance": False, "inventory": False},
        observed={"inventory"},
    ) == {"inventory"}


def test_visible_check_keys_use_enabled_snapshot_for_skipped_checks():
    assert visible_check_keys(
        {"recall": True, "open_factor": True, "relevance": False, "inventory": False},
        observed=set(),
    ) == {"recall", "open_factor"}


def test_config_change_takes_effect_on_the_next_governance_date_without_ui_version():
    previous = {"brand_configs": {"atlas": {"top_query_check_modes": {"inventory": "auto"}}}}
    candidate = {"brand_configs": {"atlas": {"top_query_check_modes": {"inventory": "disabled"}}}}
    record_check_mode_changes(candidate, previous, changed_on=__import__("datetime").date(2026, 10, 1))
    assert "version" not in candidate
    from datetime import date
    assert check_modes_for_date(candidate, "atlas", date(2026, 10, 1))["inventory"] is True
    assert check_modes_for_date(candidate, "atlas", date(2026, 10, 2))["inventory"] is False


def test_reorder_check_mode_can_disable_check_independently_of_feature_switch():
    assert effective_check_modes({
        "query_governance_reorder_enabled": True,
        "top_query_check_modes": {"open_factor": "disabled"},
    })["open_factor"] is False


def test_reenable_and_brand_history_are_independent():
    raw = {"brand_configs": {
        "atlas": {"top_query_check_modes": {"inventory": "auto"}},
        "harbor": {"top_query_check_modes": {"inventory": "auto"}},
    }}
    disabled = {"brand_configs": {
        "atlas": {"top_query_check_modes": {"inventory": "disabled"}},
        "harbor": {"top_query_check_modes": {"inventory": "auto"}},
    }}
    record_check_mode_changes(disabled, raw, changed_on=date(2026, 10, 1))
    reenabled = {"brand_configs": {
        "atlas": {"top_query_check_modes": {"inventory": "auto"}},
        "harbor": {"top_query_check_modes": {"inventory": "auto"}},
    }, "query_governance_check_history": disabled["query_governance_check_history"]}
    record_check_mode_changes(reenabled, disabled, changed_on=date(2026, 10, 3))

    assert check_modes_for_date(reenabled, "atlas", date(2026, 10, 1))["inventory"] is True
    assert check_modes_for_date(reenabled, "atlas", date(2026, 10, 2))["inventory"] is False
    assert check_modes_for_date(reenabled, "atlas", date(2026, 10, 3))["inventory"] is False
    assert check_modes_for_date(reenabled, "atlas", date(2026, 10, 4))["inventory"] is True
    assert check_modes_for_date(reenabled, "harbor", date(2026, 10, 2))["inventory"] is True
    assert "harbor" not in reenabled["query_governance_check_history"]
