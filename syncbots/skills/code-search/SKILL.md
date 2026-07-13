---
name: code-search
description: >-
  Token-efficient three-layer code retrieval protocol (glob -> grep ->
  read_file). Read this when you need to locate or understand code in the
  repository and want to minimize context consumption. Explains when to use
  each layer and how to page through large files.
---

# Three-layer code retrieval: glob -> grep -> read

Retrieval cost grows by an order of magnitude at each layer. Always start at
the cheapest layer that can answer your question, and only escalate when
needed.

## Layer 1: `glob` -- find files by name (cheapest)

Use when you need to know WHICH files exist or where a kind of file lives:

- `glob("**/*.td")` -- all TableGen sources
- `glob("lib/Conversion/**/*.cpp")` -- a subsystem's implementation files
- `glob("**/CMakeLists.txt")` -- all build files

Output is only file paths. Never `grep` or read files just to discover their
existence.

## Layer 2: `grep` -- find lines by content (cheap)

Use when you know an identifier/pattern and need WHERE it is used:

- `grep(pattern="getODSOperands", glob="*.cpp")` -- call sites of an API
- `grep(pattern="MLIRFooDialect", glob="CMakeLists.txt")` -- CMake references

Prefer narrowing with the `glob` filter and a specific `path`. Grep output
(file:line matches) is often ENOUGH to plan an edit -- e.g. counting call
sites of a renamed API. Do not read files when the match list already answers
the question.

## Layer 3: `read_file` -- read content (expensive, read on demand)

Only read what you will edit or must understand:

- ALWAYS read a region before editing it.
- Use pagination: `read_file(path, offset=<line-20>, limit=60)` around a
  grep hit or a compiler-reported line. Do NOT read a whole file unless it is
  short (< ~200 lines) or you are rewriting most of it.
- For a first look at a big file, scan the top: `read_file(path, limit=80)`
  (includes, class declarations), then jump to specific regions by offset.

## Anti-patterns (waste context)

- Reading a whole file to find one function -> `grep` for it first.
- Grepping with vague words (`Result`, `Value`, `Op`) -> use exact
  identifiers from the error message or diff digest.
- Re-reading a file you already read this iteration -> reuse what you know.
- Reading raw LLVM diffs or long logs yourself -> delegate to `diff-digest` /
  `log-analyst` subagents.
