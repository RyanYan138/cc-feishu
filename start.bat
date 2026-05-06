@echo off
pip install -r requirements.txt
:loop
python main.py
echo Bot exited, restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop
