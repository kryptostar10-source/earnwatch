# EarnWatch-Agent für Windows – meldet jede Sekunde CPU, Arbeitsspeicher und Internet an EarnWatch.
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

# Verdienst-Apps erkennen (Prozessname enthält …) – Werte aus den Windows-Leistungsdaten, ohne Admin-Rechte
$AppKeys = [ordered]@{ 'honeygain'='honeygain'; 'jumptask'='jumptask'; 'earnapp'='earnapp'; 'pawns'='pawns';
  'bitping'='bitping'; 'earn\.?fm'='earnfm'; 'wipter'='wipter'; 'bytelixir'='bytelixir'; 'grass'='grass' }
$AppWql = "SELECT Name, PercentProcessorTime, WorkingSetPrivate, IODataBytesPersec FROM Win32_PerfFormattedData_PerfProc_Process WHERE " +
  "Name LIKE '%honeygain%' OR Name LIKE '%jumptask%' OR Name LIKE '%earnapp%' OR Name LIKE '%pawns%' OR Name LIKE '%bitping%' OR " +
  "Name LIKE '%earnfm%' OR Name LIKE '%earn.fm%' OR Name LIKE '%wipter%' OR Name LIKE '%bytelixir%' OR Name LIKE '%grass%'"
$script:cores = [int](Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
if ($script:cores -lt 1) { $script:cores = 1 }

function Get-Apps {
  $sum = @{}
  foreach ($p in (Get-CimInstance -Query $AppWql)) {
    $n = ($p.Name -replace '#\d+$', '').ToLower()
    $key = $null
    foreach ($pat in $AppKeys.Keys) { if ($n -match $pat) { $key = $AppKeys[$pat]; break } }
    if (-not $key) { continue }
    if (-not $sum.ContainsKey($key)) { $sum[$key] = @{ k = $key; cpu = 0.0; mem = [int64]0; io = [int64]0; n = 0 } }
    $sum[$key].cpu += [double]$p.PercentProcessorTime / $script:cores
    $sum[$key].mem += [int64]$p.WorkingSetPrivate
    $sum[$key].io  += [int64]$p.IODataBytesPersec
    $sum[$key].n   += 1
  }
  foreach ($v in $sum.Values) { $v.cpu = [math]::Round([math]::Min(100, $v.cpu), 1) }
  ,@($sum.Values)
}

$script:static = $null
$script:staticAt = [DateTime]::MinValue

function Get-Report {
  $os = Get-CimInstance Win32_OperatingSystem -Property TotalVisibleMemorySize, FreePhysicalMemory, Caption, LastBootUpTime
  $cpu = (Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'" -Property PercentProcessorTime).PercentProcessorTime
  $stats = Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Get-NetAdapterStatistics
  $r = @{
    cpu       = [double]$cpu
    mem_total = [int64]$os.TotalVisibleMemorySize * 1024
    mem_used  = ([int64]$os.TotalVisibleMemorySize - [int64]$os.FreePhysicalMemory) * 1024
    rx        = [int64](($stats | Measure-Object -Property ReceivedBytes -Sum).Sum)
    tx        = [int64](($stats | Measure-Object -Property SentBytes -Sum).Sum)
    apps      = Get-Apps
  }
  # Feste Angaben (Name, Windows, Prozessor) nur einmal pro Minute mitschicken
  if (((Get-Date) - $script:staticAt).TotalSeconds -ge 60) {
    if (-not $script:static) {
      $script:static = @{
        host     = $env:COMPUTERNAME
        os       = $os.Caption
        cpu_name = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
        cores    = [int](Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
      }
    }
    foreach ($k in $script:static.Keys) { $r[$k] = $script:static[$k] }
    $r['uptime'] = [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds
    $script:staticAt = Get-Date
  }
  $r | ConvertTo-Json -Compress -Depth 4
}

while ($true) {
  $start = Get-Date
  try {
    $json = Get-Report
    try { Send-Report $json }
    catch [Net.WebException] {
      if (-not $script:lax -and ($_.Exception.Status -eq 'TrustFailure' -or $_.Exception.Status -eq 'SecureChannelFailure')) {
        $script:lax = $true; Send-Report $json
      } else { throw }
    }
  } catch { Start-Sleep -Seconds 5 }   # EarnWatch nicht erreichbar: kurz warten statt jede Sekunde zu versuchen
  $rest = 1000 - [int]((Get-Date) - $start).TotalMilliseconds
  if ($rest -gt 50) { Start-Sleep -Milliseconds $rest }
}
