$p = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -eq 50168 }
if ($p) {
    Write-Host ("python parent PID: " + $p.ParentProcessId)
    $par = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -eq $p.ParentProcessId }
    if ($par) {
        Write-Host ("parent name: " + $par.Name)
        Write-Host ("parent cmdline: " + $par.CommandLine)
        Write-Host ("parent's parent: " + $par.ParentProcessId)
    } else {
        Write-Host "parent non trovato"
    }
} else {
    Write-Host "bot 50168 non trovato"
}
Write-Host "=== tutti i processi con avvia_tutto o cmd ==="
Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -or $_.CommandLine -like '*avvia_tutto*' } | ForEach-Object {
    Write-Host ("PID=" + $_.ProcessId + " NAME=" + $_.Name + " CL=" + $_.CommandLine)
}
Write-Host "done"
