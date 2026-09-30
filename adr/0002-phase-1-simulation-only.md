# ADR 0002: Phase 1 is simulation only

Status: accepted.

The Phase 1 application has no aircraft command path. API state `activated` describes a simulated mission only. Telemetry and gateway concepts are read-only, and command, upload, parameter and firmware directories are absent from the initial codebase.

This prevents a development team from accidentally turning a scheduling prototype into remote aircraft control.
