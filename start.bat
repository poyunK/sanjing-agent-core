@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"

set "PY_CMD="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_CMD="%~dp0.venv\Scripts\python.exe""
) else if exist "F:\#2642Agent\.venv\Scripts\python.exe" (
    set "PY_CMD="F:\#2642Agent\.venv\Scripts\python.exe""
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PY_CMD=py -3"
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 set "PY_CMD=python"
    )
)

if not defined PY_CMD (
    echo 没找到 Python。请安装 Python 3.10 或更高版本，或配置 py -3 / python 命令。
    pause
    exit /b 1
)

:menu
echo.
echo ===== 三镜 Agent 教学原型 =====
echo 1. 离线教学模式（不需要 Key，不请求网络）
echo 2. 真实 Ark ark-code-latest＋专业数据集＋豆包搜索对话
echo 3. 恢复真实会话
echo 4. 查看最近账本
echo 5. 专业查询离线练习（合成资料，不需要 Key）
echo 6. 兼容旧 DeepSeek＋专业数据集对话
echo 7. 网页搜索离线练习（合成摘要，不需要 Key）
echo 0. 退出
set "CHOICE="
set /p "CHOICE=请输入编号并按回车："

if "%CHOICE%"=="1" (
    call %PY_CMD% -m sanjing.cli --offline --trace
    goto menu
)
if "%CHOICE%"=="2" (
    call %PY_CMD% -m sanjing.cli
    goto menu
)
if "%CHOICE%"=="3" (
    call %PY_CMD% -m sanjing.cli --resume-prompt
    goto menu
)
if "%CHOICE%"=="4" (
    call %PY_CMD% -m sanjing.cli --show-latest --open-latest
    goto menu
)
if "%CHOICE%"=="5" (
    call %PY_CMD% -m sanjing.cli --professional-demo --trace
    goto menu
)
if "%CHOICE%"=="6" (
    call %PY_CMD% -m sanjing.cli --provider deepseek
    goto menu
)
if "%CHOICE%"=="7" (
    call %PY_CMD% -m sanjing.cli --web-demo --trace
    goto menu
)
if "%CHOICE%"=="0" exit /b 0

echo 未识别的选项。
goto menu
