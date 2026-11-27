# EarnWatch-Agent für Windows – meldet jede Minute CPU, Arbeitsspeicher und Internet an EarnWatch.
# Läuft unsichtbar, braucht keine Admin-Rechte. Der Schlüssel unten wird NIE gesendet,
# jede Meldung wird damit nur unterschrieben (HMAC-SHA256).
$ErrorActionPreference = 'SilentlyContinue'
$mutex = New-Object System.Threading.Mutex($false, 'Local\EarnWatchAgent')
if (-not $mutex.WaitOne(0)) { exit }   # läuft schon
$Url = '__URL__/api/agent/__ID__'
$hex = '__KEY__'
[byte[]]$Key = for ($i = 0; $i -lt $hex.Length; $i += 2) { [Convert]::ToByte($hex.Substring($i, 2), 16) }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$script:lax = $false

function Hex([byte[]]$b) { ($b | ForEach-Object { $_.ToString('x2') }) -join '' }

function Send-Report([string]$json) {
  $bytes = [Text.Encoding]::UTF8.GetBytes($json)
  $ts = [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  $rng = New-Object byte[] 16; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($rng)
  $nonce = Hex $rng
  $bodyHash = Hex ([Security.Cryptography.SHA256]::Create().ComputeHash($bytes))
  $mac = New-Object Security.Cryptography.HMACSHA256 -ArgumentList (,$Key)
  $sig = Hex ($mac.ComputeHash([Text.Encoding]::UTF8.GetBytes("$ts|$nonce|$bodyHash")))
  $req = [Net.HttpWebRequest]::Create($Url)
  $req.Method = 'POST'; $req.ContentType = 'application/json'; $req.Timeout = 15000; $req.UserAgent = 'EarnWatch-Agent/1.0'
  $req.Headers.Add('x-ew-ts', $ts); $req.Headers.Add('x-ew-nonce', $nonce); $req.Headers.Add('x-ew-sig', $sig)
  # Heimnetz-Zertifikat (Caddy) ist nicht öffentlich prüfbar – nur dann lockern. Schutz kommt von der Signatur.
  if ($script:lax) { $req.ServerCertificateValidationCallback = { $true } }
  $req.ContentLength = $bytes.Length
  $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
  $req.GetResponse().Close()
}

function Get-Report {
  $os = Get-CimInstance Win32_OperatingSystem
  $cpu = (Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'").PercentProcessorTime
  $stats = Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Get-NetAdapterStatistics
  @{
    host      = $env:COMPUTERNAME
    os        = $os.Caption
    cpu_name  = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
    cores     = [int](Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
    cpu       = [double]$cpu
    mem_total = [int64]$os.TotalVisibleMemorySize * 1024
    mem_used  = ([int64]$os.TotalVisibleMemorySize - [int64]$os.FreePhysicalMemory) * 1024
    rx        = [int64](($stats | Measure-Object -Property ReceivedBytes -Sum).Sum)
    tx        = [int64](($stats | Measure-Object -Property SentBytes -Sum).Sum)
    uptime    = [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds
  } | ConvertTo-Json -Compress
}

while ($true) {
  try {
    $json = Get-Report
    try { Send-Report $json }
    catch [Net.WebException] {
      if (-not $script:lax -and ($_.Exception.Status -eq 'TrustFailure' -or $_.Exception.Status -eq 'SecureChannelFailure')) {
        $script:lax = $true; Send-Report $json
      }
    }
  } catch { }
  Start-Sleep -Seconds 60
}
