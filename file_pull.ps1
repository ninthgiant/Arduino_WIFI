param(
  [string]$Ip        = "192.168.1.32",
  [int]   $UdpPort   = 2390,
  [string]$Filename  = "DL260517.TXT",
  [int]   $TcpPort   = 6000,
  [string]$Out       = "pulled.bin",
  [string]$TransferId = "T02"
)

# 1. Start TCP listener
$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $TcpPort)
$listener.Start()
Write-Host "[tcp] listening on 0.0.0.0:$TcpPort"

# 2. Send START_FILE via UDP
$udp = New-Object System.Net.Sockets.UdpClient
$cmd = "START_FILE,$TransferId,$Filename,$TcpPort"
$b = [System.Text.Encoding]::ASCII.GetBytes($cmd)
$udp.Send($b, $b.Length, $Ip, $UdpPort) | Out-Null
Write-Host ">>> $cmd"

# 3. Read UDP responses in background (non-blocking poll)
$udp.Client.ReceiveTimeout = 200

# 4. Accept TCP connection
$acceptDeadline = (Get-Date).AddSeconds(10)
while (-not $listener.Pending()) {
  if ((Get-Date) -gt $acceptDeadline) {
    Write-Host "[tcp] timeout waiting for connection"
    $listener.Stop(); $udp.Close(); exit 1
  }
  # also drain udp side-channel
  try {
    $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
    $r = $udp.Receive([ref]$ep)
    Write-Host ("<<< UDP " + [System.Text.Encoding]::ASCII.GetString($r))
  } catch {}
  Start-Sleep -Milliseconds 100
}
$client = $listener.AcceptTcpClient()
Write-Host "[tcp] connection accepted from $($client.Client.RemoteEndPoint)"
$stream = $client.GetStream()

# 5. Read until EOF marker
$buffer = New-Object byte[] 65536
$outBytes = New-Object System.IO.FileStream($Out, [System.IO.FileMode]::Create)
$totalData = 0
$chunkCount = 0
$leftover = ""

$readDeadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $readDeadline) {
  if ($stream.DataAvailable -or $client.Available -gt 0) {
    $n = $stream.Read($buffer, 0, $buffer.Length)
    if ($n -le 0) { break }
    # Find header line(s) embedded in the stream
    # Headers look like CHUNK,T02,0,0,256,XXXXXXXX\n
    # We parse by scanning for \n and pulling the header out
    $cursor = 0
    while ($cursor -lt $n) {
      # find next newline
      $nl = -1
      for ($i = $cursor; $i -lt $n; $i++) {
        if ($buffer[$i] -eq 10) { $nl = $i; break }
      }
      if ($nl -lt 0) { break }   # incomplete header, give up parsing
      $line = [System.Text.Encoding]::ASCII.GetString($buffer, $cursor, $nl - $cursor)
      $cursor = $nl + 1
      if ($line.StartsWith("CHUNK,")) {
        $f = $line.Split(",")
        $bytes = [int]$f[4]
        # Read raw chunk bytes that follow
        $remaining = $bytes
        while ($remaining -gt 0) {
          # Use what's in the current buffer first
          $avail = $n - $cursor
          if ($avail -gt 0) {
            $take = [Math]::Min($avail, $remaining)
            $outBytes.Write($buffer, $cursor, $take)
            $cursor += $take
            $remaining -= $take
            $totalData += $take
          }
          if ($remaining -gt 0) {
            # Refill buffer
            $n = $stream.Read($buffer, 0, $buffer.Length)
            $cursor = 0
            if ($n -le 0) { break }
          }
        }
        $chunkCount++
        if ($chunkCount % 100 -eq 0) { Write-Host "[tcp] $chunkCount chunks, $totalData bytes" }
      } elseif ($line.StartsWith("EOF,")) {
        Write-Host "[tcp] EOF received: $line"
        $client.Close(); $listener.Stop()
        $outBytes.Close()
        Write-Host "[tcp] Total: $chunkCount chunks, $totalData bytes -> $Out"
        # Drain final UDP responses
        Start-Sleep -Milliseconds 500
        try {
          $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
          $r = $udp.Receive([ref]$ep)
          Write-Host ("<<< UDP " + [System.Text.Encoding]::ASCII.GetString($r))
        } catch {}
        $udp.Close()
        exit 0
      }
    }
  } else {
    Start-Sleep -Milliseconds 20
  }
}
Write-Host "[tcp] timeout; got $chunkCount chunks, $totalData bytes"
$client.Close(); $listener.Stop(); $outBytes.Close(); $udp.Close()
