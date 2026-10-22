from pathlib import Path


def test_slow_governance_endpoints_use_sixty_second_timeout():
    source = Path("src/integrations/operational_service.py").read_text()
    assert 'json=payload,\n                    headers=self._get_headers(brand_alias),\n                    timeout=60' in source
    assert 'headers=self._get_headers(brand),\n                        timeout=60' in source
    assert 'headers=headers,\n                    timeout=60' in source
