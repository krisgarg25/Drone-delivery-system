# ADR 0008: Vehicle link isolation

Status: proposed for Phase 5.

A future telemetry gateway will be a separate network plane with inbound-only message allowance, device provisioning, stale-data handling and no command module. Link-layer VPN or authenticated radio protection precedes MAVLink handling. MAVLink signing alone is not treated as role-based authorization because it uses a symmetric key.

The gateway must never be imported by the API or simulator.
