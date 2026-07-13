---
name: test-failure-fix
description: >-
  Playbook for fixing lit / FileCheck / ctest failures after an LLVM/MLIR
  version bump, especially `error: CHECK: expected string not found`. Read
  this when the loop diagnosis reports error_type=test_failure.
---

# Fixing test failures after an LLVM bump

1. These usually mean the tool's OUTPUT changed, not that the code is wrong. Do
   NOT delete or `XFAIL` tests to make them pass.
2. Delegate to the `check-regen` subagent: it reads each failing test's RUN line,
   re-runs the exact command to capture the new output, and rewrites the CHECK /
   CHECK-NEXT lines to match.
3. For genuine behavioral regressions (not just formatting), find the code change
   that altered behavior and decide whether the test or the code is correct.
4. After updating CHECK lines, re-read ONLY the edited region (read_file with
   offset/limit) to confirm the new patterns are internally consistent
   (labels, capture variables).
5. Batch: if the diagnosis lists many failing tests with the same pattern
   change, hand check-regen the whole list in one call.
