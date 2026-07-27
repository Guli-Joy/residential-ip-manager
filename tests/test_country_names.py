from residential_ip_manager.ui.country_names import localized_country_name


def test_localizes_iso_country_codes_to_simplified_chinese() -> None:
    assert localized_country_name("kr", "Korea Republic of") == "韩国"
    assert localized_country_name("JP", "Japan") == "日本"
    assert localized_country_name("US", "United States") == "美国"


def test_unknown_country_code_preserves_provider_fallback() -> None:
    assert localized_country_name("ZZ", "Provider Region") == "Provider Region"
    assert localized_country_name("ZZ") == "ZZ"
    assert localized_country_name("") == ""
