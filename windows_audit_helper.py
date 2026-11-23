"""Fixed inline command used by the temporary audit session's elevated worker."""

AUDIT_HELPER = r"""
$ErrorActionPreference = 'Stop'
$PSModuleAutoLoadingPreference = 'None'
$ids = @(5152, 5157)
$guids = @{
    5152 = [Guid]'0cce9225-69ae-11d9-bed3-505054503030'
    5157 = [Guid]'0cce9226-69ae-11d9-bed3-505054503030'
}
$pipe = $null
$mutex = $null
$locked = $false
$prior = @{}
$changed = @{}
$state = 'failed'
$reason = 'startup_failed'
$watch = $null
$readSecurity = __READ_SECURITY__
$backendPid = __BACKEND_PID__

function Send-Line([string]$Line) {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Line + "`n")
    $writing = $pipe.WriteAsync($bytes, 0, $bytes.Length)
    if (-not $writing.Wait(500)) { throw 'backend_exit' }
}

function Bind-Audit {
    $assembly = [AppDomain]::CurrentDomain.DefineDynamicAssembly(
        [Reflection.AssemblyName]::new('LuxEdgeAuditNative'), [Reflection.Emit.AssemblyBuilderAccess]::Run)
    $builder = $assembly.DefineDynamicModule('Native').DefineType(
        'NativeAudit', [Reflection.TypeAttributes]'Public,Abstract,Sealed')
    $advapi = [IO.Path]::Combine([Environment]::SystemDirectory, 'advapi32.dll')
    $kernel = [IO.Path]::Combine([Environment]::SystemDirectory, 'kernel32.dll')
    $methods = @{
        AuditQuerySystemPolicy = @([byte], [IntPtr], [uint32], [IntPtr])
        AuditSetSystemPolicy = @([byte], [IntPtr], [uint32])
        AuditFree = @([void], [IntPtr])
        OpenProcessToken = @([int], [IntPtr], [uint32], [IntPtr])
        LookupPrivilegeValueW = @([int], [string], [string], [IntPtr])
        AdjustTokenPrivileges = @([int], [IntPtr], [int], [IntPtr], [uint32], [IntPtr], [IntPtr])
        CloseHandle = @([int], [IntPtr])
    }
    foreach ($name in $methods.Keys) {
        $library = $advapi
        if ($name -eq 'CloseHandle') { $library = $kernel }
        $definition = $methods[$name]
        $method = $builder.DefinePInvokeMethod($name, $library,
            [Reflection.MethodAttributes]'Public,Static,PinvokeImpl', [Reflection.CallingConventions]::Standard,
            $definition[0], [type[]]$definition[1..($definition.Length - 1)],
            [Runtime.InteropServices.CallingConvention]::Winapi,
            [Runtime.InteropServices.CharSet]::Unicode)
        $method.SetImplementationFlags([Reflection.MethodImplAttributes]::PreserveSig)
    }
    return $builder.CreateType()
}

function Enable-AuditPrivilege {
    # Adjust only this elevated worker's token. The policy and Security reads verify actual access.
    $output = [Runtime.InteropServices.Marshal]::AllocHGlobal([IntPtr]::Size)
    $privilege = [Runtime.InteropServices.Marshal]::AllocHGlobal(16)
    $token = [IntPtr]::Zero
    try {
        if (-not $native::OpenProcessToken([IntPtr](-1), 0x28, $output)) { return }
        $token = [Runtime.InteropServices.Marshal]::ReadIntPtr($output)
        if (-not $native::LookupPrivilegeValueW($null, 'SeSecurityPrivilege', [IntPtr]::Add($privilege, 4))) { return }
        [Runtime.InteropServices.Marshal]::WriteInt32($privilege, 0, 1)
        [Runtime.InteropServices.Marshal]::WriteInt32($privilege, 12, 2)
        $null = $native::AdjustTokenPrivileges($token, 0, $privilege, 0, [IntPtr]::Zero, [IntPtr]::Zero)
    } finally {
        if ($token -ne [IntPtr]::Zero) { $null = $native::CloseHandle($token) }
        [Runtime.InteropServices.Marshal]::FreeHGlobal($privilege)
        [Runtime.InteropServices.Marshal]::FreeHGlobal($output)
    }
}

function Get-Policy([int]$Id) {
    $inputBuffer = [Runtime.InteropServices.Marshal]::AllocHGlobal(16)
    $output = [Runtime.InteropServices.Marshal]::AllocHGlobal([IntPtr]::Size)
    $policy = [IntPtr]::Zero
    try {
        [Runtime.InteropServices.Marshal]::Copy($guids[$Id].ToByteArray(), 0, $inputBuffer, 16)
        if (-not $native::AuditQuerySystemPolicy($inputBuffer, 1, $output)) { throw 'audit_snapshot_failed' }
        $policy = [Runtime.InteropServices.Marshal]::ReadIntPtr($output)
        if ($policy -eq [IntPtr]::Zero) { throw 'audit_snapshot_failed' }
        $value = [Runtime.InteropServices.Marshal]::ReadInt32($policy, 16)
        if ($value -notin @(0, 1, 2, 3, 4)) { throw 'audit_snapshot_failed' }
        return ($value -band 3)
    } finally {
        if ($policy -ne [IntPtr]::Zero) { $native::AuditFree($policy) }
        [Runtime.InteropServices.Marshal]::FreeHGlobal($inputBuffer)
        [Runtime.InteropServices.Marshal]::FreeHGlobal($output)
    }
}

function Set-Policy([int]$Id, [int]$Value) {
    # AUDIT_POLICY_INFORMATION has a GUID, a 32-bit flags field, then an ignored GUID.
    $buffer = [Runtime.InteropServices.Marshal]::AllocHGlobal(36)
    try {
        [Runtime.InteropServices.Marshal]::Copy([byte[]]::new(36), 0, $buffer, 36)
        [Runtime.InteropServices.Marshal]::Copy($guids[$Id].ToByteArray(), 0, $buffer, 16)
        if ($Value -eq 0) { $Value = 4 }  # POLICY_AUDIT_EVENT_NONE
        [Runtime.InteropServices.Marshal]::WriteInt32($buffer, 16, $Value)
        if (-not $native::AuditSetSystemPolicy($buffer, 1)) { throw 'audit_write_failed' }
    } finally {
        [Runtime.InteropServices.Marshal]::FreeHGlobal($buffer)
    }
}

function Apply-Audit([hashtable]$Prior, [hashtable]$Changed, [Threading.Tasks.Task]$Stop) {
    foreach ($id in @(5152, 5157)) {
        if ($Stop.IsCompleted) { throw 'cancelled' }
        if ($Prior[$id] -band 2) { continue }
        if ((Get-Policy $id) -ne $Prior[$id]) { throw 'policy_changed' }
        $expected = $Prior[$id] -bor 2
        $Changed[$id] = $expected
        Set-Policy $id $expected
        if ((Get-Policy $id) -ne $expected) { throw 'partial_apply_failed' }
    }
}

function Restore-Audit([hashtable]$Prior, [hashtable]$Changed) {
    $failed = $false
    $conflict = $false
    foreach ($id in @(5157, 5152)) {
        if (-not $Changed.ContainsKey($id)) { continue }
        try {
            $current = Get-Policy $id
            if ($current -eq $Prior[$id]) { continue }
            if ($current -ne $Changed[$id]) { $conflict = $true; continue }
            Set-Policy $id $Prior[$id]
            if ((Get-Policy $id) -ne $Prior[$id]) { $failed = $true }
        } catch { $failed = $true }
    }
    if ($failed) { return 'restore_failed' }
    if ($conflict) { return 'restore_conflict' }
    return 'restored'
}

function Open-Security {
    $hexPid = '0x{0:x}' -f $backendPid
    $query = '*[System[(EventID=5152 or EventID=5157) and TimeCreated[timediff(@SystemTime) <= 5000]] and EventData[(Data[@Name="ProcessId"]="{0}" or Data[@Name="ProcessID"]="{0}" or Data[@Name="ProcessId"]="{1}" or Data[@Name="ProcessID"]="{1}")]]' -f $backendPid, $hexPid
    $filter = [Diagnostics.Eventing.Reader.EventLogQuery]::new(
        'Security', [Diagnostics.Eventing.Reader.PathType]::LogName, $query)
    $filter.ReverseDirection = $true
    return [Diagnostics.Eventing.Reader.EventLogReader]::new($filter)
}

try {
    $pipe = [IO.Pipes.NamedPipeClientStream]::new('.', 'LuxEdgeWfp-__NONCE__',
        [IO.Pipes.PipeDirection]::InOut, [IO.Pipes.PipeOptions]::Asynchronous,
        [Security.Principal.TokenImpersonationLevel]::Anonymous)
    $pipe.Connect(5000)
    $byte = [byte[]]::new(1)
    $arming = $pipe.ReadAsync($byte, 0, 1)
    if (-not $arming.Wait(5000) -or $arming.Result -ne 1 -or $byte[0] -ne 1) { throw 'cancelled' }
    $stop = $pipe.ReadAsync($byte, 0, 1)
    $mutex = [Threading.Mutex]::new($false, 'Global\LuxEdgeWfpAuditSession')
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] {
        $locked = $true
        throw 'policy_changed'
    }
    if (-not $locked) { throw 'policy_busy' }

    $native = Bind-Audit
    Enable-AuditPrivilege
    foreach ($id in $ids) { $prior[$id] = Get-Policy $id }
    Send-Line ('prior {0} {1}' -f $prior[5152], $prior[5157])
    if ($readSecurity) {
        try {
            $reader = Open-Security
            try {
                $probe = $reader.ReadEvent([TimeSpan]::FromMilliseconds(100))
                if ($null -ne $probe) { $probe.Dispose() }
            } finally { $reader.Dispose() }
        } catch { throw 'security_read_unavailable' }
    }
    $applying = $true
    Apply-Audit $prior $changed $stop
    $applying = $false
    $appliedIds = @('none')
    if ($changed.Count) { $appliedIds = @($changed.Keys) }
    Send-Line ('ready {0} {1}' -f [int]$readSecurity, ($appliedIds -join ','))
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $lastRecord = 0L
    $checked = 0.0
    $reason = 'session_expired'
    while ($watch.Elapsed.TotalSeconds -lt __SESSION_SECONDS__) {
        if ($stop.IsCompleted) {
            $reason = 'backend_exit'
            if ($stop.Status -eq [Threading.Tasks.TaskStatus]::RanToCompletion -and $stop.Result -eq 1) {
                $reason = 'stopped'
            }
            break
        }
        if ($watch.Elapsed.TotalSeconds -ge $checked) {
            $checked = $watch.Elapsed.TotalSeconds + 5
            foreach ($id in $ids) {
                if ((Get-Policy $id) -ne ($prior[$id] -bor 2)) { throw 'policy_changed' }
            }
        }
        if ($readSecurity) {
            $latest = $lastRecord
            $reader = Open-Security
            try {
                for ($index = 0; $index -lt 64 -and -not $stop.IsCompleted; $index++) {
                    $event = $reader.ReadEvent([TimeSpan]::FromMilliseconds(100))
                    if ($null -eq $event) { break }
                    try {
                        if ($event.RecordId -le $lastRecord) { break }
                        $latest = [Math]::Max($latest, [long]$event.RecordId)
                        $xml = [Text.Encoding]::UTF8.GetBytes($event.ToXml())
                        if ($xml.Length -le 49152) { Send-Line ('event ' + [Convert]::ToBase64String($xml)) }
                    } finally { $event.Dispose() }
                }
            } finally { $reader.Dispose() }
            $lastRecord = $latest
            if ($index -eq 64) { Send-Line 'tick limit' } else { Send-Line 'tick' }
        }
        [Threading.Thread]::Sleep(400)
    }
    $state = 'restored'
} catch {
    $reason = $_.Exception.Message
    if ($reason -eq 'cancelled') { $state = 'cancelled' }
    if ($reason -eq 'policy_changed' -and $watch) { $state = 'restore_conflict' }
    if ($reason -notin @('cancelled','audit_snapshot_failed','security_read_unavailable','policy_changed',
            'policy_busy','partial_apply_failed','backend_exit')) {
        $reason = 'startup_failed'
        if ($applying) { $reason = 'partial_apply_failed' }
    }
} finally {
    $restore = Restore-Audit $prior $changed
    if ($restore -eq 'restore_conflict') { $state = 'restore_conflict'; $reason = 'policy_changed' }
    if ($restore -eq 'restore_failed') { $state = 'restore_failed'; $reason = 'restore_failed' }
    if ($null -ne $pipe) {
        try { Send-Line ('result ' + $state + ' ' + $reason) } catch {}
        $pipe.Dispose()
    }
    if ($locked) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
switch ($state) {
    'restored' { exit 0 }
    'restore_conflict' { exit 21 }
    'restore_failed' { exit 22 }
    'cancelled' { exit 23 }
    default { exit 24 }
}
"""
