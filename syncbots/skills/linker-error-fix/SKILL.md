---
name: linker-error-fix
description: >-
  Playbook for fixing linker failures after an LLVM/MLIR version bump:
  `undefined reference`, `multiple definition`, `cannot find -lX`, `undefined
  symbol`. Read this when the loop diagnosis reports error_type=linker_error.
---

# Fixing linker errors after an LLVM bump

1. A symbol is declared but not defined (or defined twice).
2. Common causes after an LLVM bump:
   - A function moved to a different library -> update `target_link_libraries`
     in the relevant CMakeLists.txt.
   - A header was split -> include the new header that declares the symbol.
   - A library target was renamed (e.g. `MLIRFoo` -> `MLIRFooDialect`).
3. Use `grep` to find where the missing target/symbol is referenced in CMake
   (`grep` pattern = target name, glob = `CMakeLists.txt`); only `read_file`
   the matching sections.
4. Ask `diff-digest` which library the symbol moved to if it is not obvious.
