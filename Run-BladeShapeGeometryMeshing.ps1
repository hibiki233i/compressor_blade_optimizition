param (
    [Parameter(Mandatory=$true)][string]$CandidateJson,
    [string]$WorkingDir = "",
    [string]$CFturboExe = "C:\Program Files\CFturbo 2025.2.2\CFturbo.exe",
    [string]$TurboGridExe = "D:\ANSYS Inc\v251\TurboGrid\bin\cfxtg.exe",
    [string]$CftBatchTemplate = "D:\blade optizamation\Templates\BaseModel.cft-batch",
    [string]$BaseCft = "D:\blade optizamation\Templates\0908-2.cft",
    [string]$TurboGridTemplate = "D:\blade optizamation\Templates\BaseMeshing.tst",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Set-ChildText {
    param(
        [xml]$Xml,
        [System.Xml.XmlElement]$Parent,
        [string]$Name,
        [string]$Value,
        [hashtable]$Attributes = @{}
    )
    $node = $Parent.SelectSingleNode($Name)
    if ($null -eq $node) {
        $node = $Xml.CreateElement($Name)
        [void]$Parent.AppendChild($node)
    }
    foreach ($key in $Attributes.Keys) {
        $node.SetAttribute($key, [string]$Attributes[$key])
    }
    $node.InnerText = $Value
    return $node
}

function Set-IndexedValue {
    param(
        [System.Xml.XmlNode]$Parent,
        [int]$Index,
        [double]$Value
    )
    $node = $Parent.SelectSingleNode("Value[@Index='$Index']")
    if ($null -eq $node) {
        throw "Missing Value node with Index=$Index under $($Parent.Name)"
    }
    $node.InnerText = $Value.ToString("G17", [System.Globalization.CultureInfo]::InvariantCulture)
}

function Set-VectorY {
    param(
        [System.Xml.XmlNode]$ValueNode,
        [double]$Value
    )
    $yNode = $ValueNode.SelectSingleNode("y")
    if ($null -eq $yNode) {
        throw "Missing y node in vector value index $($ValueNode.Index)"
    }
    $yNode.InnerText = $Value.ToString("G17", [System.Globalization.CultureInfo]::InvariantCulture)
}

function Ensure-TurboGridExportAction {
    param(
        [xml]$Xml,
        [string]$ExportDir
    )
    $project = $Xml.SelectSingleNode("//CFturboBatchProject")
    if ($null -eq $project) {
        throw "CFturbo batch template missing CFturboBatchProject node."
    }
    $project.SetAttribute("InputFile", ".\input_model.cft")
    $exportAction = $project.SelectSingleNode("BatchAction[@Name='Export']")
    if ($null -eq $exportAction) {
        $exportAction = $Xml.CreateElement("BatchAction")
        $exportAction.SetAttribute("Type", "Object")
        $exportAction.SetAttribute("Name", "Export")
        [void]$project.AppendChild($exportAction)
    }
    [void](Set-ChildText -Xml $Xml -Parent $exportAction -Name "WorkingDir" -Value $ExportDir)
    [void](Set-ChildText -Xml $Xml -Parent $exportAction -Name "BaseFileName" -Value "Impeller")
    [void](Set-ChildText -Xml $Xml -Parent $exportAction -Name "ExportInterface" -Value "TurboGrid" -Attributes @{ Type = "Enum" })

    $components = $exportAction.SelectSingleNode("ExportComponents")
    if ($null -eq $components) {
        $components = $Xml.CreateElement("ExportComponents")
        [void]$exportAction.AppendChild($components)
    } else {
        $components.RemoveAll()
    }
    $components.SetAttribute("Count", "1")
    $components.SetAttribute("Type", "Array1")
    $components.SetAttribute("Desc", "Components to be exported")
    $value = $Xml.CreateElement("Value")
    $value.SetAttribute("Type", "Integer")
    $value.SetAttribute("Caption", "[Impeller_1]")
    $value.SetAttribute("Index", "0")
    $value.InnerText = "1"
    [void]$components.AppendChild($value)
}

function Set-TurboGridExportSettings {
    param(
        [string]$CftPath,
        [string]$ExportDir
    )
    if (-not (Test-Path $CftPath)) {
        throw "CFturbo project file not found: $CftPath"
    }
    $cftXml = [xml](Get-Content -Path $CftPath -Raw)
    $tgInterface = $cftXml.SelectSingleNode("//TTurboGridExportInterface")
    if ($null -eq $tgInterface) {
        Write-Warning "Base .cft has no TTurboGridExportInterface; continuing with batch export settings only."
        return
    }
    [void](Set-ChildText -Xml $cftXml -Parent $tgInterface -Name "BaseFileName" -Value "Impeller")
    [void](Set-ChildText -Xml $cftXml -Parent $tgInterface -Name "ExportDir" -Value $ExportDir)
    [void](Set-ChildText -Xml $cftXml -Parent $tgInterface -Name "TurboGridFormat" -Value "tgfTSE" -Attributes @{ Type = "Enum" })
    $cftXml.Save($CftPath)
}

function Test-CurveExport {
    param([string]$Hub, [string]$Shroud, [string]$Profile)
    return ((Test-Path $Hub) -and (Test-Path $Shroud) -and (Test-Path $Profile))
}

function Write-TurboGridFiles {
    param(
        [string]$TurboGridTemplate,
        [string]$CurrentTgs,
        [string]$CurrentTse,
        [string]$HubCurve,
        [string]$ShroudCurve,
        [string]$ProfileCurve,
        [string]$OutputMesh,
        [int]$BladeCount = 10,
        [double]$TipClearance = 0.0013
    )
    $tgsContent = Get-Content -Path $TurboGridTemplate -Raw
    $periodicAngle = 360.0 / [double]$BladeCount
    $tgsContent = $tgsContent -replace "\{BLADE_COUNT\}", $BladeCount.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    $tgsContent = $tgsContent -replace "\{HUB_CURVE\}", ($HubCurve -replace "\\", "/")
    $tgsContent = $tgsContent -replace "\{SHROUD_CURVE\}", ($ShroudCurve -replace "\\", "/")
    $tgsContent = $tgsContent -replace "\{PROFILE_CURVE\}", ($ProfileCurve -replace "\\", "/")
    $tgsContent = $tgsContent -replace "\{OUTPUT_MESH\}", ($OutputMesh -replace "\\", "/")
    $tgsContent = $tgsContent -replace "\{PERIODIC_ANGLE\}", $periodicAngle.ToString("F5", [System.Globalization.CultureInfo]::InvariantCulture)
    $tgsContent = $tgsContent -replace "\{TIP_CLEARANCE\}", $TipClearance.ToString("F6", [System.Globalization.CultureInfo]::InvariantCulture)
    $tgsContent = [regex]::Replace(
        $tgsContent,
        "(?m)^(\s*State Filename\s*=\s*).+$",
        '${1}' + ($CurrentTgs -replace "\\", "/")
    )
    Set-Content -Path $CurrentTgs -Value $tgsContent -Encoding UTF8

    $tgsPath = $CurrentTgs -replace "\\", "/"
    $gtmPath = $OutputMesh -replace "\\", "/"
    $tseContent = @"
> um object=/TOPOLOGY SET, mode=normal, update=off
>readstate filename=$tgsPath, mode = \
append, load = false
> update
>savemesh filename=$gtmPath, coorddata=Off, \
onedomain=true, single=Off, units=m, solver=cfx5
> update
>quit
"@
    Set-Content -Path $CurrentTse -Value $tseContent -Encoding UTF8
}

function Set-MeanLineShape {
    param(
        [xml]$Xml,
        [int]$LineIndex,
        [double[]]$Beta,
        [double]$Theta
    )
    if ($Beta.Count -ne 5) {
        throw "Mean-line beta array must contain exactly 5 values."
    }
    $line = $Xml.SelectSingleNode("//Updates//TMeanLine[@Index='$LineIndex']")
    if ($null -eq $line) {
        throw "Missing Updates TMeanLine Index=$LineIndex"
    }
    $inner = $line.SelectSingleNode("InnerProgPoints")
    if ($null -ne $inner) {
        Set-VectorY -ValueNode $inner.SelectSingleNode("Value[@Index='0']") -Value $Beta[1]
        Set-VectorY -ValueNode $inner.SelectSingleNode("Value[@Index='1']") -Value $Beta[2]
        Set-VectorY -ValueNode $inner.SelectSingleNode("Value[@Index='2']") -Value $Beta[3]
    }
    $prog = $line.SelectSingleNode("ProgPoints")
    if ($null -ne $prog) {
        for ($i = 0; $i -lt 5; $i++) {
            Set-VectorY -ValueNode $prog.SelectSingleNode("Value[@Index='$i']") -Value $Beta[$i]
        }
    }
    $lePos = $line.SelectSingleNode("lePos")
    if ($null -ne $lePos) {
        $lePos.InnerText = $Theta.ToString("G17", [System.Globalization.CultureInfo]::InvariantCulture)
    }
    $stacking = $line.SelectSingleNode("StackingPhi")
    if ($null -ne $stacking) {
        $stacking.InnerText = $Theta.ToString("G17", [System.Globalization.CultureInfo]::InvariantCulture)
    }
}

if (-not (Test-Path $CandidateJson)) {
    throw "Candidate JSON not found: $CandidateJson"
}

$candidate = Get-Content -Path $CandidateJson -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($WorkingDir)) {
    $WorkingDir = Split-Path -Parent $CandidateJson
}
New-Item -ItemType Directory -Path $WorkingDir -Force | Out-Null

$Current_CFT = Join-Path $WorkingDir "run_cfturbo.cft-batch"
$Input_CFT_Model = Join-Path $WorkingDir "input_model.cft"
$Current_TGS = Join-Path $WorkingDir "run_turbogrid.tst"
$Current_TSE = Join-Path $WorkingDir "run_turbogrid.tse"
$Export_Hub = Join-Path $WorkingDir "Impeller_hub.curve"
$Export_Shroud = Join-Path $WorkingDir "Impeller_shroud.curve"
$Export_Profile = Join-Path $WorkingDir "Impeller_profile.curve"
$Export_Mesh = Join-Path $WorkingDir "Impeller_Mesh.gtm"
$cftLogPath = Join-Path $WorkingDir "run_cfturbo.log"
$cftExportLogPath = Join-Path $WorkingDir "run_cfturbo_export.log"
$Modified_CFT_Model = Join-Path $WorkingDir "0908-2_modified.cft"

Copy-Item -Path $BaseCft -Destination $Input_CFT_Model -Force
Copy-Item -Path $BaseCft -Destination (Join-Path $WorkingDir "0908-2.cft") -Force
Set-TurboGridExportSettings -CftPath $Input_CFT_Model -ExportDir $WorkingDir

$xml = [xml](Get-Content -Path $CftBatchTemplate -Raw)

$hubBeta = [double[]]@($candidate.geometry.hub_beta_rad)
$shroudBeta = [double[]]@($candidate.geometry.shroud_beta_rad)
$hubTheta = [double]$candidate.geometry.hub_theta_rad
$shroudTheta = [double]$candidate.geometry.shroud_theta_rad

Set-MeanLineShape -Xml $xml -LineIndex 0 -Beta $hubBeta -Theta $hubTheta
Set-MeanLineShape -Xml $xml -LineIndex 1 -Beta $shroudBeta -Theta $shroudTheta

$blade = $xml.SelectSingleNode("//Updates//BladePropsML[@Name='Main blade']")
if ($null -eq $blade) {
    throw "Missing Main blade update node."
}
Set-IndexedValue -Parent $blade.SelectSingleNode("Beta1") -Index 0 -Value $hubBeta[0]
Set-IndexedValue -Parent $blade.SelectSingleNode("Beta1") -Index 1 -Value $shroudBeta[0]
Set-IndexedValue -Parent $blade.SelectSingleNode("Beta2") -Index 0 -Value $hubBeta[4]
Set-IndexedValue -Parent $blade.SelectSingleNode("Beta2") -Index 1 -Value $shroudBeta[4]

Ensure-TurboGridExportAction -Xml $xml -ExportDir $WorkingDir
$xml.Save($Current_CFT)

if ($DryRun) {
    Write-TurboGridFiles `
        -TurboGridTemplate $TurboGridTemplate `
        -CurrentTgs $Current_TGS `
        -CurrentTse $Current_TSE `
        -HubCurve $Export_Hub `
        -ShroudCurve $Export_Shroud `
        -ProfileCurve $Export_Profile `
        -OutputMesh $Export_Mesh
    Write-Host "Dry-run complete. Wrote $Current_CFT, $Input_CFT_Model, $Current_TGS, and $Current_TSE"
    exit 0
}

if (Test-Path $Export_Mesh) {
    Remove-Item $Export_Mesh -Force
}

$cftProcess = Start-Process -FilePath $CFturboExe -ArgumentList "-batch `"$Current_CFT`" -verbose -log `"$cftLogPath`"" -WorkingDirectory $WorkingDir -Wait -PassThru -NoNewWindow
if ($cftProcess.ExitCode -ne 0 -and $cftProcess.ExitCode -ne 1) {
    throw "CFturbo failed with exit code $($cftProcess.ExitCode). See $cftLogPath"
}

if (-not (Test-CurveExport -Hub $Export_Hub -Shroud $Export_Shroud -Profile $Export_Profile)) {
    if (Test-Path $Modified_CFT_Model) {
        Set-TurboGridExportSettings -CftPath $Modified_CFT_Model -ExportDir $WorkingDir
        $exportProcess = Start-Process -FilePath $CFturboExe -ArgumentList "-batch `"$Modified_CFT_Model`" -export TurboGrid -verbose -log `"$cftExportLogPath`"" -WorkingDirectory $WorkingDir -Wait -PassThru -NoNewWindow
        if ($exportProcess.ExitCode -ne 0 -and $exportProcess.ExitCode -ne 1) {
            Write-Warning "Secondary CFturbo export failed with exit code $($exportProcess.ExitCode)."
        }
    }
}

if (-not (Test-CurveExport -Hub $Export_Hub -Shroud $Export_Shroud -Profile $Export_Profile)) {
    throw "CFturbo did not export TurboGrid curves. Expected $Export_Hub, $Export_Shroud, and $Export_Profile"
}

$logContent = if (Test-Path $cftLogPath) { Get-Content $cftLogPath -Raw } else { "" }
$fatalKeywords = @(
    "choked flow",
    "Thermodynamic state cannot be calculated",
    "Suction diameter dS < choking diameter",
    "cm-calculation failed",
    "Grid generation or cm-calculation failed",
    "Calculation of LE blade angles not possible",
    "A reasonable thermodynamic state could not be calculated"
)
foreach ($keyword in $fatalKeywords) {
    if ($logContent -match [regex]::Escape($keyword)) {
        throw "CFturbo fatal warning detected: $keyword"
    }
}

Write-TurboGridFiles `
    -TurboGridTemplate $TurboGridTemplate `
    -CurrentTgs $Current_TGS `
    -CurrentTse $Current_TSE `
    -HubCurve $Export_Hub `
    -ShroudCurve $Export_Shroud `
    -ProfileCurve $Export_Profile `
    -OutputMesh $Export_Mesh

$tgProcess = Start-Process -FilePath $TurboGridExe -ArgumentList "-batch `"$Current_TSE`"" -WorkingDirectory $WorkingDir -Wait -PassThru -NoNewWindow
if ($tgProcess.ExitCode -ne 0) {
    throw "TurboGrid failed with exit code $($tgProcess.ExitCode)."
}
if (-not (Test-Path $Export_Mesh)) {
    throw "TurboGrid finished but mesh was not found: $Export_Mesh"
}
Write-Host "Mesh generated: $Export_Mesh"
