@echo off
setlocal
set "CALLTABLE_ROOT=%~dp0.."
set "PYTHONPATH=%CALLTABLE_ROOT%;%PYTHONPATH%"
python -m calltable cli %*
