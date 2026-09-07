param(
    [string]$BaseUrl = 'http://127.0.0.1:8787',
    [string]$Username = 'demo',
    [string]$Password = 'demo'
)

$ErrorActionPreference = 'Stop'
$davUrl = "$BaseUrl/dav/"
$movieDirectoryHref = '/dav/%E7%94%B5%E5%BD%B1/'
$seriesDirectoryHref = '/dav/%E7%94%B5%E8%A7%86%E5%89%A7/'

$options = & curl.exe --silent --show-error --fail --request OPTIONS $davUrl
if ($LASTEXITCODE -ne 0) {
    throw 'WebDAV OPTIONS failed'
}

$rootListing = & curl.exe --silent --show-error --fail `
    --user "${Username}:${Password}" `
    --request PROPFIND `
    --header 'Depth: 1' `
    $davUrl
if ($LASTEXITCODE -ne 0) {
    throw 'Root WebDAV PROPFIND failed'
}
$rootXml = [xml]($rootListing -join "`n")
$rootHrefs = @($rootXml.SelectNodes('//*[local-name()="response"]/*[local-name()="href"]') | ForEach-Object { $_.InnerText })
if ($rootHrefs -notcontains $movieDirectoryHref -or $rootHrefs -notcontains $seriesDirectoryHref) {
    throw 'Root WebDAV PROPFIND did not list both movie and series directories'
}

$movieDavUrl = "$BaseUrl$movieDirectoryHref"
$movieListing = & curl.exe --silent --show-error --fail `
    --user "${Username}:${Password}" `
    --request PROPFIND `
    --header 'Depth: 1' `
    $movieDavUrl
if ($LASTEXITCODE -ne 0) {
    throw 'Movie WebDAV PROPFIND failed'
}
$movieXml = [xml]($movieListing -join "`n")
$movieHrefs = @($movieXml.SelectNodes('//*[local-name()="response"]/*[local-name()="href"]') | ForEach-Object { $_.InnerText })
$strmHref = $movieHrefs | Where-Object { $_ -match '\.strm$' } | Select-Object -First 1
if (-not $strmHref -or ($movieHrefs | Where-Object { $_ -match '\.strm$' }).Count -ne 100) {
    throw "Movie WebDAV directory did not contain exactly 100 STRM files"
}

$strmUrl = & curl.exe --silent --show-error --fail `
    --user "${Username}:${Password}" `
    "$BaseUrl$strmHref"
if ($LASTEXITCODE -ne 0 -or $strmUrl -notmatch '/play\?') {
    throw 'WebDAV GET did not return a /play URL'
}
$strmUrl = $strmUrl.Trim()

$handler = [System.Net.Http.HttpClientHandler]::new()
$handler.AllowAutoRedirect = $true
$http = [System.Net.Http.HttpClient]::new($handler)
$request = [System.Net.Http.HttpRequestMessage]::new(
    [System.Net.Http.HttpMethod]::Get,
    $strmUrl
)
$request.Headers.Range = [System.Net.Http.Headers.RangeHeaderValue]::new(0, 65535)
$response = $null
try {
    $response = $http.SendAsync(
        $request,
        [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
    ).GetAwaiter().GetResult()
    $response.EnsureSuccessStatusCode() | Out-Null
    $contentType = $response.Content.Headers.ContentType.MediaType
    $finalUrl = $response.RequestMessage.RequestUri.AbsoluteUri

    if ($contentType -eq 'video/mp4') {
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $prefix = [byte[]]::new(12)
        $read = $stream.Read($prefix, 0, $prefix.Length)
        if (
            $read -lt 8 -or
            [Text.Encoding]::ASCII.GetString($prefix, 4, 4) -ne 'ftyp'
        ) {
            throw 'The direct MP4 response did not contain an ftyp file header'
        }
        $playbackMode = 'mp4-direct'
        $hlsHeader = $null
        $segmentMode = $null
        $segmentLength = $read
        $magic = ($prefix[0..([Math]::Min(11, $read - 1))] | ForEach-Object {
            $_.ToString('X2')
        }) -join ' '
    } else {
        $manifestText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        $manifest = $manifestText -split "`r?`n"
        if ($manifest[0] -ne '#EXTM3U') {
            throw "The /play target returned unsupported content type $contentType"
        }
        $segmentUrl = $manifest | Where-Object {
            $_ -and -not $_.StartsWith('#')
        } | Select-Object -First 1
        if (-not $segmentUrl) {
            throw 'No segment URL was found in the HLS manifest'
        }
        $segment = $http.GetByteArrayAsync($segmentUrl).GetAwaiter().GetResult()
        $magic = ($segment[0..3] | ForEach-Object { $_.ToString('X2') }) -join ' '
        $segmentMode = if ($segmentUrl -match '/segment\?') { 'proxy' } else { 'direct' }
        if ($segmentMode -eq 'proxy' -and $segment[0] -ne 0x47) {
            throw "Expected proxied MPEG-TS sync byte 47, got $magic"
        }
        if (
            $segmentMode -eq 'direct' -and
            ($segment.Length -lt 8 -or
             $segment[0] -ne 0x89 -or
             $segment[1] -ne 0x50 -or
             $segment[2] -ne 0x4E -or
             $segment[3] -ne 0x47)
        ) {
            throw "Expected direct upstream PNG wrapper, got $magic"
        }
        $playbackMode = 'hls'
        $hlsHeader = $manifest[0]
        $segmentLength = $segment.Length
    }
} finally {
    if ($null -ne $response) { $response.Dispose() }
    $request.Dispose()
    $http.Dispose()
}

[pscustomobject]@{
    WebDavUrl = $davUrl
    RootResources = $rootHrefs.Count
    MovieStrmFiles = ($movieHrefs | Where-Object { $_ -match '\.strm$' }).Count
    SelectedStrmHref = $strmHref
    StrmUrl = $strmUrl
    FinalUrl = $finalUrl
    PlaybackMode = $playbackMode
    HlsHeader = $hlsHeader
    SegmentMode = $segmentMode
    FirstMediaBytes = $segmentLength
    FirstMediaMagic = $magic
}
