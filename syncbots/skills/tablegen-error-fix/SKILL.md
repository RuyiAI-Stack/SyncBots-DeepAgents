---
name: tablegen-error-fix
description: >-
  Playbook for fixing TableGen-related failures after an LLVM/MLIR version
  bump: errors in generated `*.inc` / `*.cpp.inc` / `*.h.inc` files or during
  TableGen processing. Read this when the loop diagnosis reports
  error_type=tablegen_error.
---

# Fixing TableGen errors after an LLVM bump

1. NEVER edit generated `*.inc` files. They are regenerated from `.td` sources.
2. Trace the error back to the `.td` file that defines the failing Op/interface:
   `glob` for `**/*.td`, then `grep` the Op/interface name to find its
   definition, and `read_file` only that region.
3. Common causes:
   - An Op interface gained/renamed a required method -> update the Op definition
     in the `.td`.
   - A new mandatory field/trait was added upstream.
   - A TableGen backend changed its expected syntax.
4. If the `.td` lives inside the LLVM submodule, do NOT edit it -- the downstream
   project must adapt its own `.td`/C++ instead.
