@echo off
rem A process the puller can execute in place of `hf`: it takes the same
rem arguments and hands them to a python that behaves like the CLI. `_hf_cli`
rem returns a path, so the substitution is one line in the test.
rem Prefer ATHENA_PY, fall back to the interpreter in this machine's Hermes install.
if not defined ATHENA_PY set "ATHENA_PY=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
"%ATHENA_PY%" "%~dp0pull_stub.py" %*
exit /b %ERRORLEVEL%
