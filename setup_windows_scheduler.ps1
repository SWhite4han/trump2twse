# 在 Windows PowerShell (以系統管理員身份) 執行此腳本
# 建立 Windows Task Scheduler 任務，取代 WSL2 內的 crontab
# 效果：即使 WSL2 被 idle shutdown，Windows 也會自動喚醒 WSL2 並執行 pipeline
#
# 使用方式：
#   右鍵 PowerShell → 以系統管理員身份執行
#   cd <此專案的 Windows 路徑>
#   .\setup_windows_scheduler.ps1

$TaskName    = "MarketTrackDailyPipeline"
$Description = "Market Track 每日台股情報 pipeline（透過 WSL2 執行）"

# WSL distro 名稱（執行 wsl -l 確認）
$WslDistro   = "Ubuntu"

# WSL 內的指令
$WslCmd = "bash -lc 'cd /home/white/projects/market-track && .venv/bin/python scripts/daily_pipeline.py --shadow >> logs/cron.log 2>&1'"

$Action = New-ScheduledTaskAction `
    -Execute "wsl.exe" `
    -Argument "-d $WslDistro -- $WslCmd"

# 週一到週五 09:00 台北時間（UTC+8）= 請確認 Windows 時區設定為台北
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "09:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `   # 若電腦在排程時間關機，開機後補跑
    -RunOnlyIfNetworkAvailable

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# 移除舊任務（若存在）
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Description $Description `
    -Action      $Action `
    -Trigger     $Trigger `
    -Settings    $Settings `
    -Principal   $Principal `
    -Force

Write-Host ""
Write-Host "✅ Task '$TaskName' 建立成功" -ForegroundColor Green
Write-Host "   排程：週一～五 09:00（台北時間）"
Write-Host "   確認方式：schtasks /query /tn $TaskName /fo LIST /v"
Write-Host ""
Write-Host "建立後請停用 WSL2 內的 crontab（避免重複執行）："
Write-Host "   crontab -e  →  刪除或註解 daily_pipeline 那一行"
