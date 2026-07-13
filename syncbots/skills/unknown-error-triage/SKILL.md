---
name: unknown-error-triage
description: >-
  Triage playbook for build/test failures that could not be classified
  (error_type=unknown). Read this to identify the real error type, then jump
  to the matching fix skill (compile-error-fix, linker-error-fix,
  tablegen-error-fix, cmake-error-fix, test-failure-fix).
---

# Triaging an unclassified failure

1. Focus on the FIRST error in the log. Later errors are usually cascading.
2. If the condensed log is confusing, pass the full log path to the
   `log-analyst` subagent for a root-cause diagnosis instead of reading the
   raw log yourself.
3. Route by origin:
   - Error in a generated `*.inc` file -> trace back to the `.td` source
     (see `tablegen-error-fix`).
   - Error inside the LLVM submodule or `third_party/` -> the downstream
     project must adapt its own code; do not edit vendored sources.
   - `undefined reference` / `ld returned` -> `linker-error-fix`.
   - `CMake Error` -> `cmake-error-fix`.
   - FileCheck / lit failure -> `test-failure-fix`.
   - Compiler diagnostics (`x.cpp:L:C: error:`) -> `compile-error-fix`.
4. Read the failing file (targeted region only) before editing.
