@echo off
rem Opens emcosim in the browser with the example project. Close this window to stop it.
set ROOT=%~dp0..
"%ROOT%\.venv\Scripts\python.exe" -m emcosim --project "%ROOT%\examples\switch_robot" serve
pause
