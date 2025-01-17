$Content = (Get-Content -Path $PSScriptRoot\devices.json) -join ""
$Devices = ConvertFrom-Json $Content

$MarkdownColumns = @"
| {0} | {1} | {2} | {3} | {4} |
|-----|-----|-----|-----|-----|
"@ -f "Image", "Vencor", "Model", "Name", "Description"

$BaseUrl = "https://www.zigbee2mqtt.io"
$MarkdownRows = ""
foreach ($Device in $Devices)
{
    $Model = $Device.model -replace "/", "_"
    $ModelImg = $Device.model -replace "/", "-"
    $Url = "$BaseUrl/devices/$Model.html"
    $ImgUrl = "$BaseUrl/images/devices/$ModelImg.jpg"
    $MarkdownRow = "| ![img]({0}) | {1} | [{2}]({3}) | {4} | {5} |" -f $ImgUrl, $Device.vendor, $Device.model, $Url, $Device.friendly_name, $Device.description
    $MarkdownRows += $MarkdownRow + "`n"
}

$Markdown = @($MarkdownColumns, $MarkdownRows) -join "`n"
$Markdown | Out-File $PSScriptRoot\README.md
