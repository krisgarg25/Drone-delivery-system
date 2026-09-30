"""Phase 1 worker boundary. It may project observations, never command aircraft."""

from droneops_contracts import MissionStatus

WORKER_CAPABILITIES = ("projection", "refresh", "report")
PROHIBITED_CAPABILITIES = ("vehicle_command", "firmware_upload", "mission_upload")

__all__ = ["PROHIBITED_CAPABILITIES", "WORKER_CAPABILITIES", "MissionStatus"]
