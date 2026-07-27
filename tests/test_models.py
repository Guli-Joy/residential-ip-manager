from residential_ip_manager.domain.models import ComponentCheck, EnvironmentReport


def test_environment_requires_only_required_checks() -> None:
    report = EnvironmentReport(
        checks=[
            ComponentCheck("openvpn", "OpenVPN", True, "ok"),
            ComponentCheck("controller", "Clash API", False, "optional", required=False),
        ]
    )

    assert report.ready
