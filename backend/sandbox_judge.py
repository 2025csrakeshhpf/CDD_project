import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Dict, List

ALL_15_LANGUAGES = [
    ("python", "Python", [sys.executable]),
    ("c", "C", ["gcc", "clang"]),
    ("cpp", "C++", ["g++", "clang++"]),
    ("java", "Java", ["javac"]),
    ("javascript", "JavaScript", ["node"]),
    ("typescript", "TypeScript", ["ts-node", "tsc"]),
    ("csharp", "C#", ["dotnet", "csc", "mcs"]),
    ("php", "PHP", ["php"]),
    ("ruby", "Ruby", ["ruby"]),
    ("go", "Go", ["go"]),
    ("kotlin", "Kotlin", ["kotlinc"]),
    ("swift", "Swift", ["swift"]),
    ("rust", "Rust", ["rustc"]),
    ("r", "R", ["Rscript"]),
    ("sql", "SQL", ["__sqlite_builtin__"]),
]


def detect_supported_languages() -> List[Dict]:
    judge0_url = os.environ.get("JUDGE0_URL", "").strip()
    results = []
    for slug, label, bins in ALL_15_LANGUAGES:
        if slug == "sql":
            results.append({"slug": slug, "display_name": label, "is_supported": True, "runtime_info": "SQLite 3 In-Memory Engine"})
            continue
        found = None
        for b in bins:
            if b == sys.executable or shutil.which(b):
                found = b
                break
        if found:
            results.append({"slug": slug, "display_name": label, "is_supported": True, "runtime_info": f"Local Sandbox ({os.path.basename(found)})"})
        elif judge0_url:
            results.append({"slug": slug, "display_name": label, "is_supported": True, "runtime_info": "Judge0 Remote Sandbox"})
        else:
            results.append({"slug": slug, "display_name": label, "is_supported": False, "runtime_info": "Runtime not installed on host"})
    return results


def _clean_env() -> Dict[str, str]:
    allowed = ("PATH", "SystemRoot", "TEMP", "TMP", "WINDIR", "HOME")
    return {k: v for k, v in os.environ.items() if k in allowed}


def _exec_sql_sandbox(sql_code: str, stdin_setup: str) -> Dict:
    start = time.perf_counter()
    try:
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        if stdin_setup.strip():
            cur.executescript(stdin_setup)
        statements = [s.strip() for s in sql_code.strip().split(";") if s.strip()]
        rows_out = []
        for stmt in statements:
            cur.execute(stmt)
            if cur.description:
                for r in cur.fetchall():
                    rows_out.append(" ".join(str(x) for x in r))
        conn.close()
        ms = round((time.perf_counter() - start) * 1000.0, 2)
        return {"status": "OK", "output": "\n".join(rows_out).strip(), "time_ms": ms}
    except Exception as exc:
        ms = round((time.perf_counter() - start) * 1000.0, 2)
        return {"status": "RUNTIME ERROR", "output": str(exc), "time_ms": ms}


def _run_cmd(cmd: List[str], stdin_data: str, cwd: str, timeout_sec: float = 2.5) -> Dict:
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=_clean_env(),
            timeout=timeout_sec,
        )
        ms = round((time.perf_counter() - start) * 1000.0, 2)
        out = (proc.stdout or "")[:4096].strip()
        err = (proc.stderr or "")[:4096].strip()
        if proc.returncode != 0:
            return {"status": "RUNTIME ERROR", "output": err or f"Exit code {proc.returncode}", "time_ms": ms}
        return {"status": "OK", "output": out, "time_ms": ms}
    except subprocess.TimeoutExpired:
        return {"status": "TIME LIMIT EXCEEDED", "output": f"Execution exceeded {timeout_sec}s limit.", "time_ms": round(timeout_sec * 1000.0, 2)}
    except Exception as exc:
        return {"status": "RUNTIME ERROR", "output": str(exc), "time_ms": 0.0}


def execute_sandboxed_code(language: str, code: str, test_cases: List[Dict]) -> Dict:
    lang = language.strip().lower()
    if not code or not code.strip():
        return {
            "verdict": "COMPILATION ERROR",
            "total_cases": len(test_cases),
            "passed_cases": 0,
            "failed_cases": len(test_cases),
            "execution_time_ms": 0.0,
            "error_message": "Editor is empty. Please write your solution before running or submitting.",
            "case_results": [],
        }

    with tempfile.TemporaryDirectory(prefix="pt_sandbox_") as tmpdir:
        run_cmd: List[str] = []

        if lang == "sql":
            pass
        elif lang == "python":
            p = os.path.join(tmpdir, "main.py")
            open(p, "w", encoding="utf-8").write(code)
            run_cmd = [sys.executable, "-I", p]
        elif lang == "javascript":
            node = shutil.which("node")
            if not node:
                return _unavail("JavaScript (node)", test_cases)
            p = os.path.join(tmpdir, "main.js")
            open(p, "w", encoding="utf-8").write(code)
            run_cmd = [node, p]
        elif lang == "typescript":
            ts_node = shutil.which("ts-node")
            tsc = shutil.which("tsc")
            node = shutil.which("node")
            p = os.path.join(tmpdir, "main.ts")
            open(p, "w", encoding="utf-8").write(code)
            if ts_node:
                run_cmd = [ts_node, p]
            elif tsc and node:
                comp = subprocess.run([tsc, p, "--outDir", tmpdir], capture_output=True, text=True, cwd=tmpdir, timeout=10)
                if comp.returncode != 0:
                    return _comp_err(comp.stderr, test_cases)
                run_cmd = [node, os.path.join(tmpdir, "main.js")]
            else:
                return _unavail("TypeScript", test_cases)
        elif lang == "c":
            gcc = shutil.which("gcc") or shutil.which("clang")
            if not gcc:
                return _unavail("C (gcc)", test_cases)
            src = os.path.join(tmpdir, "main.c")
            exe = os.path.join(tmpdir, "main.exe" if os.name == "nt" else "main")
            open(src, "w", encoding="utf-8").write(code)
            comp = subprocess.run([gcc, "-O2", src, "-o", exe], capture_output=True, text=True, cwd=tmpdir, timeout=10)
            if comp.returncode != 0:
                return _comp_err(comp.stderr, test_cases)
            run_cmd = [exe]
        elif lang == "cpp":
            gpp = shutil.which("g++") or shutil.which("clang++")
            if not gpp:
                return _unavail("C++ (g++)", test_cases)
            src = os.path.join(tmpdir, "main.cpp")
            exe = os.path.join(tmpdir, "main.exe" if os.name == "nt" else "main")
            open(src, "w", encoding="utf-8").write(code)
            comp = subprocess.run([gpp, "-std=c++17", "-O2", src, "-o", exe], capture_output=True, text=True, cwd=tmpdir, timeout=10)
            if comp.returncode != 0:
                return _comp_err(comp.stderr, test_cases)
            run_cmd = [exe]
        elif lang == "java":
            javac, java = shutil.which("javac"), shutil.which("java")
            if not javac or not java:
                return _unavail("Java (JDK)", test_cases)
            src = os.path.join(tmpdir, "Main.java")
            open(src, "w", encoding="utf-8").write(code)
            comp = subprocess.run([javac, src], capture_output=True, text=True, cwd=tmpdir, timeout=12)
            if comp.returncode != 0:
                return _comp_err(comp.stderr, test_cases)
            run_cmd = [java, "-Xmx128m", "-cp", tmpdir, "Main"]
        elif lang == "php":
            php = shutil.which("php")
            if not php:
                return _unavail("PHP", test_cases)
            p = os.path.join(tmpdir, "main.php")
            open(p, "w", encoding="utf-8").write(code)
            run_cmd = [php, p]
        elif lang == "ruby":
            ruby = shutil.which("ruby")
            if not ruby:
                return _unavail("Ruby", test_cases)
            p = os.path.join(tmpdir, "main.rb")
            open(p, "w", encoding="utf-8").write(code)
            run_cmd = [ruby, p]
        elif lang == "go":
            go = shutil.which("go")
            if not go:
                return _unavail("Go", test_cases)
            p = os.path.join(tmpdir, "main.go")
            open(p, "w", encoding="utf-8").write(code)
            run_cmd = [go, "run", p]
        elif lang == "rust":
            rustc = shutil.which("rustc")
            if not rustc:
                return _unavail("Rust", test_cases)
            src = os.path.join(tmpdir, "main.rs")
            exe = os.path.join(tmpdir, "main.exe" if os.name == "nt" else "main")
            open(src, "w", encoding="utf-8").write(code)
            comp = subprocess.run([rustc, src, "-o", exe], capture_output=True, text=True, cwd=tmpdir, timeout=12)
            if comp.returncode != 0:
                return _comp_err(comp.stderr, test_cases)
            run_cmd = [exe]
        elif lang == "kotlin":
            kotlinc, java = shutil.which("kotlinc"), shutil.which("java")
            if not kotlinc or not java:
                return _unavail("Kotlin", test_cases)
            src = os.path.join(tmpdir, "Main.kt")
            jar = os.path.join(tmpdir, "main.jar")
            open(src, "w", encoding="utf-8").write(code)
            comp = subprocess.run([kotlinc, src, "-include-runtime", "-d", jar], capture_output=True, text=True, cwd=tmpdir, timeout=15)
            if comp.returncode != 0:
                return _comp_err(comp.stderr, test_cases)
            run_cmd = [java, "-jar", jar]
        elif lang == "swift":
            swift = shutil.which("swift")
            if not swift:
                return _unavail("Swift", test_cases)
            p = os.path.join(tmpdir, "main.swift")
            open(p, "w", encoding="utf-8").write(code)
            run_cmd = [swift, p]
        elif lang == "r":
            rscript = shutil.which("Rscript")
            if not rscript:
                return _unavail("R", test_cases)
            p = os.path.join(tmpdir, "main.R")
            open(p, "w", encoding="utf-8").write(code)
            run_cmd = [rscript, p]
        elif lang == "csharp":
            csc = shutil.which("csc") or shutil.which("mcs")
            if not csc:
                return _unavail("C#", test_cases)
            src = os.path.join(tmpdir, "Program.cs")
            exe = os.path.join(tmpdir, "Program.exe")
            open(src, "w", encoding="utf-8").write(code)
            comp = subprocess.run([csc, src, f"-out:{exe}"], capture_output=True, text=True, cwd=tmpdir, timeout=12)
            if comp.returncode != 0:
                return _comp_err(comp.stderr, test_cases)
            run_cmd = [exe]
        else:
            return _unavail(language, test_cases)

        passed_cnt, failed_cnt = 0, 0
        max_ms = 0.0
        verdict = "ACCEPTED"
        err_msg = ""
        case_results = []

        for idx, tc in enumerate(test_cases):
            is_sample = bool(tc.get("is_sample", True))
            res = _exec_sql_sandbox(code, tc["input"]) if lang == "sql" else _run_cmd(run_cmd, tc["input"], tmpdir)
            max_ms = max(max_ms, res["time_ms"])
            exp = tc["expected"].strip()
            act = res["output"].strip()

            if res["status"] != "OK":
                failed_cnt += 1
                if verdict == "ACCEPTED":
                    verdict = res["status"]
                    err_msg = res["output"]
                case_results.append({
                    "case_num": idx + 1,
                    "type": "Sample" if is_sample else "Hidden",
                    "passed": False,
                    "status": res["status"],
                    "input": tc["input"] if is_sample else "[Hidden Input]",
                    "expected": exp if is_sample else "[Hidden]",
                    "actual": act if is_sample else res["status"],
                    "time_ms": res["time_ms"],
                })
            elif act == exp:
                passed_cnt += 1
                case_results.append({
                    "case_num": idx + 1,
                    "type": "Sample" if is_sample else "Hidden",
                    "passed": True,
                    "status": "PASSED",
                    "input": tc["input"] if is_sample else "[Hidden Input]",
                    "expected": exp if is_sample else "[Hidden]",
                    "actual": act if is_sample else "[Matched]",
                    "time_ms": res["time_ms"],
                })
            else:
                failed_cnt += 1
                if verdict == "ACCEPTED":
                    verdict = "WRONG ANSWER"
                    err_msg = f"Failed on {'Sample' if is_sample else 'Hidden'} Test Case #{idx + 1}"
                case_results.append({
                    "case_num": idx + 1,
                    "type": "Sample" if is_sample else "Hidden",
                    "passed": False,
                    "status": "WRONG ANSWER",
                    "input": tc["input"] if is_sample else "[Hidden Input]",
                    "expected": exp if is_sample else "[Hidden]",
                    "actual": act if is_sample else "[Mismatch]",
                    "time_ms": res["time_ms"],
                })

        return {
            "verdict": verdict,
            "total_cases": len(test_cases),
            "passed_cases": passed_cnt,
            "failed_cases": failed_cnt,
            "execution_time_ms": round(max_ms, 2),
            "error_message": err_msg,
            "case_results": case_results,
        }


def _comp_err(stderr: str, test_cases: List[Dict]) -> Dict:
    return {
        "verdict": "COMPILATION ERROR",
        "total_cases": len(test_cases),
        "passed_cases": 0,
        "failed_cases": len(test_cases),
        "execution_time_ms": 0.0,
        "error_message": (stderr or "Compilation failed.")[:2000],
        "case_results": [],
    }


def _unavail(label: str, test_cases: List[Dict]) -> Dict:
    return {
        "verdict": "COMPILATION ERROR",
        "total_cases": len(test_cases),
        "passed_cases": 0,
        "failed_cases": len(test_cases),
        "execution_time_ms": 0.0,
        "error_message": f"{label} compiler/runtime is not installed on this server.",
        "case_results": [],
    }
