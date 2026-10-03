@echo off
rem Single entry for the local gateway. Double-click for the menu, or: prism.cmd gui|start|login|status
chcp 65001 >nul
cd /d "%~dp0"
set "CMD=%~1"
set "FROM_MENU="
if not "%CMD%"=="" goto dispatch

:menu
set "FROM_MENU=1"
set "CMD="
title Prism Bridge
cls
echo ==============================================================
echo   Prism Bridge 本地网关
echo ==============================================================
echo.
echo   [1] 图形界面（登录、启动、日志都在一个窗口）
echo   [2] 启动服务（控制台窗口，端口 18765）
echo   [3] 登录
echo   [4] 查看状态
echo   [0] 退出
echo.
choice /c 12340 /n /m "按数字键选择: "
if errorlevel 255 exit /b 1
if errorlevel 5 exit /b 0
if errorlevel 4 set "CMD=status"& goto dispatch
if errorlevel 3 set "CMD=login"& goto dispatch
if errorlevel 2 set "CMD=start"& goto dispatch
if errorlevel 1 set "CMD=gui"& goto dispatch
exit /b 1

:dispatch
if /i "%CMD%"=="gui" goto gui
if /i "%CMD%"=="start" goto start
if /i "%CMD%"=="serve" goto start
if /i "%CMD%"=="login" goto login
if /i "%CMD%"=="status" goto status
echo 用法: prism.cmd [gui ^| start ^| login ^| status]
exit /b 2

:gui
start "" pythonw gui.py
exit /b 0

:start
title Prism Bridge - 本地服务 (端口 18765)
echo ==============================================================
echo [Prism Bridge] 启动本地服务 (监听 127.0.0.1:18765)...
echo 正在唤醒 Chromium 与 Prism 工作区沙箱，请稍候约 15-25 秒...
echo ==============================================================
echo.
python -u bridge.py serve
set "EXIT_CODE=%errorlevel%"
if "%EXIT_CODE%"=="0" goto done
echo.
echo ==============================================================
echo [启动失败] 退出代码: %EXIT_CODE%
echo 可能的原因:
echo   1. 登录凭证已过期 - 重新运行 prism.cmd 选择登录
echo   2. 网络连接异常   - 请检查能否访问 prism.openai.com
echo   3. 端口 18765 被占用 - 请关闭占用该端口的程序
echo ==============================================================
goto done

:login
title Prism Bridge - 自动化登录
echo ==============================================================
echo [Prism Bridge] 正在唤起浏览器进行登录...
echo ==============================================================
python -u bridge.py login
goto done

:status
title Prism Bridge - 状态面板
python -u bridge.py status
goto done

:done
echo.
pause
if defined FROM_MENU goto menu
