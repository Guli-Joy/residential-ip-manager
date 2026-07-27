from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    ENVIRONMENT_NOT_READY = "ENVIRONMENT_NOT_READY"
    VPNGATE_UNAVAILABLE = "VPNGATE_UNAVAILABLE"
    NO_STRICT_HOME_NODE = "NO_STRICT_HOME_NODE"
    CLASH_NOT_FOUND = "CLASH_NOT_FOUND"
    CLASH_PORT_UNAVAILABLE = "CLASH_PORT_UNAVAILABLE"
    OPENVPN_NOT_FOUND = "OPENVPN_NOT_FOUND"
    OPENVPN_CONNECT_FAILED = "OPENVPN_CONNECT_FAILED"
    OPENVPN_AUTH_FAILED = "OPENVPN_AUTH_FAILED"
    EXIT_IP_MISMATCH = "EXIT_IP_MISMATCH"
    NETWORK_RESTORE_FAILED = "NETWORK_RESTORE_FAILED"
    OPERATION_CANCELLED = "OPERATION_CANCELLED"


class AppError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail
