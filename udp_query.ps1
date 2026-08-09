# Send a UDP command and wait for reply on the same socket.
# Usage: .\udp_query.ps1 -Ip 192.168.1.32 -Port 2390 -Msg POLL_UID
param([string]$Ip = "192.168.1.32", [int]$Port = 2390, [string]$Msg = "POLL_UID", [int]$WaitMs = 3000)
$c = New-Object System.Net.Sockets.UdpClient
$c.Client.ReceiveTimeout = $WaitMs
$b = [System.Text.Encoding]::ASCII.GetBytes($Msg)
$c.Send($b, $b.Length, $Ip, $Port) | Out-Null
Write-Host ">>> $Msg to ${Ip}:${Port}"
$ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
try {
  $resp = $c.Receive([ref]$ep)
  Write-Host ("<<< {0}:{1}: {2}" -f $ep.Address, $ep.Port, [System.Text.Encoding]::ASCII.GetString($resp))
} catch {
  Write-Host "(no reply within ${WaitMs}ms)"
}
$c.Close()
