"""Unit tests for the deterministic verifier (error classification + condensing)."""

from __future__ import annotations

from syncbots.loop.verifier import (
    _extract_error_lines,
    _strip_filecheck_dump,
    classify_error,
)


def test_classify_compile_error():
    log = "src/foo.cpp:42:9: error: no member named 'getResult' in 'mlir::Op'"
    assert classify_error(log) == "compile_error"


def test_classify_linker_error():
    log = "ld: undefined reference to `mlir::doThing()'"
    assert classify_error(log) == "linker_error"


def test_classify_tablegen_error_precedence():
    # .inc errors must classify as tablegen, not compile, even with file:line:col
    log = "build/include/Foo.cpp.inc:10:3: error: unknown type"
    assert classify_error(log) == "tablegen_error"


def test_classify_cmake_error():
    log = "CMake Error: Unknown CMake command add_mlir_dialect_library"
    assert classify_error(log) == "cmake_error"


def test_classify_test_failure():
    log = "FAIL: stablehlo :: ops.mlir (1 of 200)"
    assert classify_error(log) == "test_failure"


def test_classify_unknown():
    assert classify_error("") == "unknown"
    assert classify_error("just some random text") == "unknown"


def test_strip_filecheck_dump():
    log = "error: CHECK failed\n<<<<<<\nlots of IR\nmore IR\n>>>>>>\ntail line"
    out = _strip_filecheck_dump(log)
    assert "lots of IR" not in out
    assert "error: CHECK failed" in out
    assert "tail line" in out


def test_extract_error_lines_keeps_compiler_error():
    lines = ["noise"] * 500
    lines.append("src/a.cpp:10:2: error: no matching function for call to 'foo'")
    lines += ["more noise"] * 500
    big = "\n".join(lines)
    out = _extract_error_lines(big, max_chars=2000)
    assert "no matching function" in out
    assert len(out) <= 2000 + 200  # allow small header overhead
