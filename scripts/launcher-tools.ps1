function Resolve-NativeApplication {
    [OutputType([string])]
    param(
        [Parameter(Mandatory)]
        [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
        [string]$Name
    )

    $executableName = "$Name.exe"
    $commands = @(
        Get-Command `
            -Name $executableName `
            -CommandType Application `
            -All `
            -ErrorAction Stop
    )
    $command = $commands |
        Where-Object { $_.Name -ieq $executableName } |
        Select-Object -First 1
    if ($null -eq $command) {
        throw "Unable to locate the native Windows application $executableName."
    }

    $item = Get-Item -LiteralPath $command.Source -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        $target = $item.ResolveLinkTarget($true)
        if ($null -eq $target) {
            throw "Unable to resolve the native application link $($item.FullName)."
        }
        $item = $target
    }
    if ($item.PSIsContainer -or $item.Extension -ine '.exe') {
        throw "$($item.FullName) is not a native Windows .exe."
    }
    return $item.FullName
}
