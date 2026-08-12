@echo off
chcp 65001 >nul
echo ============================================================
echo   Stock Tracker · 前端交易驾驶舱 (web/)
echo ============================================================
echo.
echo   前端为纯静态文件（HTML/CSS/JS，零构建），由后端
echo   http.server 从 web/ 目录统一托管，无需单独启动前端。
echo.
echo   请使用后端一键启动脚本拉起服务：
echo.
echo       scripts\start.bat        (Windows 一键启动后端 + 静态托管)
echo.
echo   启动成功后，在浏览器访问：
echo.
echo       http://localhost:8080/
echo.
echo   本文件仅作说明。真正的服务进程由后端 scripts\start.bat 创建。
echo   若后端已在运行，直接打开上面的地址即可看到交易驾驶舱。
echo.
echo   数据模式横幅（页面顶部）会如实显示：
echo     LIVE    真实行情
echo     DEGRADED 数据降级（部分源异常/熔断）
echo   每条行情/信号卡片均标注 data_status 与观察年龄，延迟/过期数据不会伪装成实时。
echo.
pause
