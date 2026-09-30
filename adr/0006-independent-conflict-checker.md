# ADR 0006: Independent conflict checker

Status: proposed for Phase 2.

The planner and the conflict checker must use separate implementations and different assumptions where practical. The checker reads committed records, raw geometry and the active policy, then returns SAFE, CONFLICT or UNPROVEN. Any planner-approved case that the checker flags blocks release until explained and resolved.

This is our strongest software defense against shared bugs and overconfident geometry.
