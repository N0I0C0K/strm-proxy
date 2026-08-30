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
if ($LASTEXITCODE -ne 0 -or $strmUrl -notmatch '/hls\.m3u8\?') {
    throw 'WebDAV GET did not return a HLS URL'
}
$strmUrl = $strmUrl.Trim()

$manifest = & curl.exe --silent --show-error --fail $strmUrl
if ($LASTEXITCODE -ne 0 -or $manifest[0] -ne '#EXTM3U') {
    throw 'The STRM target did not return an HLS manifest'
}
$segmentUrl = $manifest | Where-Object { $_ -and -not $_.StartsWith('#') } | Select-Object -First 1
if (-not $segmentUrl) {
    throw 'No segment URL was found in the HLS manifest'
}

$http = [System.Net.Http.HttpClient]::new()
try {
    $segment = $http.GetByteArrayAsync($segmentUrl).GetAwaiter().GetResult()
} finally {
    $http.Dispose()
}
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

[pscustomobject]@{
    WebDavUrl = $davUrl
    RootResources = $rootHrefs.Count
    MovieStrmFiles = ($movieHrefs | Where-Object { $_ -match '\.strm$' }).Count
    SelectedStrmHref = $strmHref
    StrmUrl = $strmUrl
    HlsHeader = $manifest[0]
    SegmentMode = $segmentMode
    FirstSegmentBytes = $segment.Length
    FirstSegmentMagic = $magic
}
