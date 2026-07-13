---
name: compile-error-fix
description: >-
  Playbook for fixing C/C++ compilation failures after an LLVM/MLIR version
  bump: `error:`, `no member named`, `no matching function`, `undeclared
  identifier`, `cannot convert`. Read this when the loop diagnosis reports
  error_type=compile_error.
---

# Fixing compile errors after an LLVM bump

1. Focus on the FIRST compiler error; later errors are usually cascading.
2. Locate context cheaply before reading: `grep` the failing symbol to find all
   call sites, then `read_file` ONLY the failing file around the reported line
   (use offset/limit). Do not read whole files.
3. Map the symptom to a cause:
   - `no member named 'X'` -> the API was renamed or moved. Confirm the new name
     via the `diff-digest` subagent (do not read raw LLVM headers yourself).
   - `no matching function for call to 'X'` -> signature changed; check the new
     parameter list / overloads.
   - `use of undeclared identifier` -> a header moved or a symbol was renamed;
     update the `#include` or the symbol.
   - `cannot convert 'A' to 'B'` -> a type changed (e.g. `Type` vs `TypedValue`);
     adapt the call site.
4. Apply the minimal edit with `edit_file`, then move to the next distinct error.
5. Fix ALL occurrences of the same renamed API in one pass (`grep` for the old
   identifier repo-wide first).
6. Do not silence errors by deleting code paths; adapt them to the new API.
