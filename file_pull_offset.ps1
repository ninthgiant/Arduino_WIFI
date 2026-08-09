param([int]$TcpPort=6002, [int]$Offset=400000, [string]$Filename="DL260517.TXT", [string]$Tid="F2", [string]$Ip="192.168.1.32")
$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $TcpPort)
$listener.Start()
$udp = New-Object System.Net.Sockets.UdpClient
$cmd = "START_FILE,$Tid,$Filename,$TcpPort,$Offset"
$b = [System.Text.Encoding]::ASCII.GetBytes($cmd)
$udp.Send($b, $b.Length, $Ip, 2390) | Out-Null
Write-Host ">>> $cmd"
$end = (Get-Date).AddSeconds(8)
while (-not $listener.Pending()) {
  if ((Get-Date) -gt $end) { Write-Host "timeout"; $listener.Stop(); $udp.Close(); exit }
  Start-Sleep -Milliseconds 100
}
$client = $listener.AcceptTcpClient()
$stream = $client.GetStream()
Start-Sleep -Milliseconds 1500
$buf = New-Object byte[] 8192
$total = 0
while ($stream.DataAvailable) {
  $n = $stream.Read($buf, 0, 8192)
  if ($n -le 0) { break }
  $total += $n
}
Write-Host "TCP got $total bytes"
$client.Close(); $listener.Stop()
$udp.Client.ReceiveTimeout = 500
$ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
try { $r = $udp.Receive([ref]$ep); Write-Host ("UDP <<< " + [System.Text.Encoding]::ASCII.GetString($r)) } catch {}
$udp.Close()
