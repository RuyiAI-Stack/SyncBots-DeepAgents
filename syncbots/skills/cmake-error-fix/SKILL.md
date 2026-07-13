---
name: cmake-error-fix
description: >-
  Playbook for fixing CMake/build-system failures after an LLVM/MLIR version
  bump: `CMake Error`, `Unknown CMake command`, `Could not find ... module`,
  `No rule to make target`. Read this when the loop diagnosis reports
  error_type=cmake_error.
---

# Fixing CMake errors after an LLVM bump

1. `grep` the failing CMake command/module name across `CMakeLists.txt` and
   `cmake/*.cmake` files first; only `read_file` the matching files.
2. Check CMakeLists.txt for renamed CMake helpers, e.g. `add_mlir_dialect_library`,
   `mlir_tablegen`, `add_public_tablegen_target`.
3. Check `find_package(MLIR ...)` / `find_package(LLVM ...)` and any
   `include(...)` of LLVM/MLIR CMake modules that may have moved or renamed.
4. New upstream components may require additional `LLVM_LINK_COMPONENTS` or
   dependency declarations.
5. Verify cached variables (e.g. `MLIR_DIR`, `LLVM_DIR`) still point at valid
   paths after the bump.
