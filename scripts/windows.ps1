$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload)) | ConvertFrom-Json
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WXReader {
 [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left,Top,Right,Bottom; }
 [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X,Y; }
 public delegate bool EnumProc(IntPtr h,IntPtr p);
 [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f,IntPtr p);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h,StringBuilder s,int n);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h,out RECT r);
 [DllImport("user32.dll")] public static extern bool ScreenToClient(IntPtr h,ref POINT p);
 [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h,IntPtr dc,uint flags);
 [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern bool PostMessage(IntPtr h,uint msg,IntPtr wp,IntPtr lp);
}
"@
[void][WXReader]::SetProcessDPIAware()
$script:actionAttempted=$false
$script:inputPostedCount=0

function Get-WindowInfo([IntPtr]$h) {
 [uint32]$windowPid=0
 [void][WXReader]::GetWindowThreadProcessId($h,[ref]$windowPid)
 $process=Get-Process -Id $windowPid
 if ($process.ProcessName -notin @('Weixin','WeChat')) { throw 'Target is not a WeChat process' }
 $title=New-Object Text.StringBuilder 512
 [void][WXReader]::GetWindowText($h,$title,512)
 $r=New-Object WXReader+RECT
 if (-not [WXReader]::GetWindowRect($h,[ref]$r)) { throw 'Window rectangle unavailable' }
 return [pscustomobject]@{handle=$h.ToInt64();pid=$windowPid;title=$title.ToString();left=$r.Left;top=$r.Top;width=$r.Right-$r.Left;height=$r.Bottom-$r.Top;minimized=[WXReader]::IsIconic($h);version=(Get-Item $process.Path).VersionInfo.ProductVersion}
}

function Get-WeChatWindows {
 $script:windows=[Collections.Generic.List[object]]::new()
 $script:processIds=@(Get-Process Weixin,WeChat -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
 $callback=[WXReader+EnumProc]{param($h,$unused)
  [uint32]$windowPid=0
  [void][WXReader]::GetWindowThreadProcessId($h,[ref]$windowPid)
  if ($script:processIds -contains $windowPid -and [WXReader]::IsWindowVisible($h)) {
   try { $script:windows.Add((Get-WindowInfo $h)) } catch { }
  }
  return $true
 }
 [void][WXReader]::EnumWindows($callback,[IntPtr]::Zero)
 return $script:windows.ToArray()
}

function Get-Bitmap($info) {
 if ($info.minimized) { throw 'Window is minimized; restore WeChat before capturing' }
 if ($info.width -le 0 -or $info.height -le 0 -or $info.width -gt 12000 -or $info.height -gt 12000) { throw 'Invalid capture dimensions' }
 $bitmap=New-Object Drawing.Bitmap ($info.width),($info.height),([Drawing.Imaging.PixelFormat]::Format24bppRgb)
 $graphics=[Drawing.Graphics]::FromImage($bitmap)
 $dc=$graphics.GetHdc()
 try { $ok=[WXReader]::PrintWindow([IntPtr]$info.handle,$dc,2) }
 finally { $graphics.ReleaseHdc($dc);$graphics.Dispose() }
 if (-not $ok) { $bitmap.Dispose();throw 'PrintWindow failed' }
 return $bitmap
}

function Get-ImageHash($bitmap,$region) {
 $rect=[Drawing.Rectangle]::new([int]$region[0],[int]$region[1],[int]$region[2],[int]$region[3])
 if ($rect.X -lt 0 -or $rect.Y -lt 0 -or $rect.Width -le 0 -or $rect.Height -le 0 -or $rect.Right -gt $bitmap.Width -or $rect.Bottom -gt $bitmap.Height) { throw 'Guard rectangle outside image' }
 $crop=$bitmap.Clone($rect,$bitmap.PixelFormat)
 $stream=[IO.MemoryStream]::new()
 $sha=[Security.Cryptography.SHA256]::Create()
 try {
  $crop.Save($stream,[Drawing.Imaging.ImageFormat]::Png)
  return ([BitConverter]::ToString($sha.ComputeHash($stream.ToArray()))).Replace('-','').ToLowerInvariant()
 } finally { $sha.Dispose();$stream.Dispose();$crop.Dispose() }
}

function Send-WindowMessage([IntPtr]$h,[uint32]$message,[long]$wparam,[long]$lparam) {
 $script:actionAttempted=$true
 if (-not [WXReader]::PostMessage($h,$message,[IntPtr]$wparam,[IntPtr]$lparam)) { throw 'Posting window input failed' }
 $script:inputPostedCount++
}

try {
 if ($p.action -eq 'status') {
  [pscustomobject]@{ok=$true;windows=@(Get-WeChatWindows)} | ConvertTo-Json -Depth 6 -Compress
  exit 0
 }
 $session=$p.session;$chat=$p.chat;$guard=$p.guard
 if ($p.action -ne 'capture') { $session=$p.expected.session }
 if (-not $session) { $session=Join-Path ([IO.Path]::GetTempPath()) ('codex-wechat\read-'+[Guid]::NewGuid().ToString('N')) }
 if ($session -notmatch '^[A-Za-z]:[\\/]') { throw 'Session must be an absolute drive-letter Windows path' }
 $session=[IO.Path]::GetFullPath($session)
 [void][IO.Directory]::CreateDirectory($session)
 $image=Join-Path $session ($p.capture_id+'.png')
 $snapshot=Join-Path $session ($p.capture_id+'.json')
 if ((Test-Path -LiteralPath $image) -or (Test-Path -LiteralPath $snapshot)) { throw 'Refusing to overwrite capture' }
 $probe=Join-Path $session ($p.capture_id+'.probe')
 $probeStream=[IO.File]::Open($probe,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
 $probeStream.Dispose()
 [IO.File]::Delete($probe)
 if ($p.action -eq 'capture') {
  $matches=@(Get-WeChatWindows | Where-Object {$_.title -ceq $p.window})
  if ($matches.Count -ne 1) { throw "Expected one matching WeChat window; found $($matches.Count)" }
  $info=$matches[0]
 } else {
  $expected=$p.expected
  $info=Get-WindowInfo ([IntPtr]$expected.window.handle)
  foreach ($key in @('handle','pid','title','left','top','width','height','version')) {
   if ($info.$key -cne $expected.window.$key) { throw "Window changed ($key); capture and inspect again" }
  }
  $chat=$expected.chat_scope;$guard=$expected.guard
  if ($p.action -in @('click','scroll') -and ($p.x -lt 0 -or $p.y -lt 0 -or $p.x -ge $info.width -or $p.y -ge $info.height)) { throw 'Coordinates outside window' }
  if ($p.action -in @('click','scroll') -and ($p.x -lt $guard[0] -or $p.y -lt $guard[1] -or $p.x -ge ($guard[0]+$guard[2]) -or $p.y -ge ($guard[1]+$guard[3]))) { throw 'Action target outside the verified screenshot region' }
  if ($p.action -eq 'search-text' -and ($guard[0] -ne 0 -or $guard[1] -ne 0 -or $guard[2] -ne $info.width -or $guard[3] -ne $info.height)) { throw 'Search text requires full-window verification' }
  $before=Get-Bitmap $info
  try { $hash=Get-ImageHash $before $guard } finally { $before.Dispose() }
  if ($hash -ne $expected.guard_hash) { throw 'Screen changed; capture and inspect again before navigation' }
  $h=[IntPtr]$info.handle
  if ($p.action -in @('click','scroll')) {
   if ($p.x -lt 0 -or $p.y -lt 0 -or $p.x -ge $info.width -or $p.y -ge $info.height) { throw 'Coordinates outside window' }
   $point=New-Object WXReader+POINT
   $point.X=$info.left+$p.x;$point.Y=$info.top+$p.y
   if ($p.action -eq 'click') {
    if (-not [WXReader]::ScreenToClient($h,[ref]$point)) { throw 'Cannot map coordinates to client' }
    $lp=(($point.Y -band 65535) -shl 16) -bor ($point.X -band 65535)
    Send-WindowMessage $h 0x0201 1 $lp
    Send-WindowMessage $h 0x0202 0 $lp
    if ($p.double) {
     Send-WindowMessage $h 0x0203 1 $lp
     Send-WindowMessage $h 0x0202 0 $lp
    }
   } else {
    $lp=(($point.Y -band 65535) -shl 16) -bor ($point.X -band 65535)
    $delta=120*[Math]::Sign($p.steps)
    for ($i=0;$i -lt [Math]::Abs($p.steps);$i++) { Send-WindowMessage $h 0x020A (($delta -band 65535) -shl 16) $lp }
   }
  } elseif ($p.action -eq 'search-text') {
   foreach ($character in $p.text.ToCharArray()) {
    if ([int]$character -lt 32) { throw 'Control characters forbidden in search' }
    Send-WindowMessage $h 0x0102 ([int]$character) 1
   }
  } else { throw 'Unsupported action' }
  Start-Sleep -Milliseconds 650
  $info=Get-WindowInfo $h
 }
 $bitmap=Get-Bitmap $info
 try {
  if (-not $guard) { $guard=@(0,0,$info.width,$info.height) }
  $guardHash=Get-ImageHash $bitmap $guard
  $imageHash=Get-ImageHash $bitmap @(0,0,$info.width,$info.height)
  if (Test-Path -LiteralPath $image) { throw 'Refusing to overwrite capture' }
  $bitmap.Save($image,[Drawing.Imaging.ImageFormat]::Png)
 } finally { $bitmap.Dispose() }
 [pscustomobject]@{ok=$true;action=$p.action;action_attempted=$script:actionAttempted;input_posted_count=$script:inputPostedCount;captured_at=[DateTime]::UtcNow.ToString('o');session=$session;chat_scope=$chat;window=$info;guard=$guard;guard_hash=$guardHash;image_hash=$imageHash;image=$image;snapshot=$snapshot} | ConvertTo-Json -Depth 6 -Compress
} catch {
 [pscustomobject]@{ok=$false;action=$p.action;action_attempted=$script:actionAttempted;input_posted_count=$script:inputPostedCount;error=$_.Exception.Message} | ConvertTo-Json -Compress
 exit 1
}
