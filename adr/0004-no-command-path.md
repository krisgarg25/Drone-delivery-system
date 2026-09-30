# ADR 0004: No command path in the initial artifact

Status: accepted.

The API and worker packages are forbidden by CI from importing gateway or vehicle command modules. The OpenAPI contract is tested for command, upload, parameter, firmware, takeoff, land, return and launch operations. Future command capability requires a new ADR, threat model, independent review, aviation authority evidence and a separate deployment boundary.
