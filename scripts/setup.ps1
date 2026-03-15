# Fortress v4 - Setup Script (Windows PowerShell)
# Ejecutar como: .\scripts\setup.ps1

param(
    [string]$BasePath = "E:\Proyectos\BotsDeTrading"
)

Write-Host "Fortress v4 - Setup Script" -ForegroundColor Green
Write-Host "==========================" -ForegroundColor Green
Write-Host ""

# Verificar Python
$pythonVersion = python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python no está instalado o no está en el PATH"
    exit 1
}
Write-Host "Python detectado: $pythonVersion" -ForegroundColor Cyan

# Crear directorios
Write-Host "Creando directorios..." -ForegroundColor Yellow
$dirs = @(
    "$BasePath\fortress_v4",
    "$BasePath\fortress_runtime\data\raw",
    "$BasePath\fortress_runtime\data\processed",
    "$BasePath\fortress_runtime\runs",
    "$BasePath\fortress_runtime\reports",
    "$BasePath\fortress_runtime\logs",
    "$BasePath\fortress_runtime\cache",
    "$BasePath\fortress_runtime\state",
    "$BasePath\fortress_secrets"
)

foreach ($dir in $dirs) {
    if (!(Test-Path $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Write-Host "  Creado: $dir" -ForegroundColor Gray
    } else {
        Write-Host "  Ya existe: $dir" -ForegroundColor DarkGray
    }
}

# Configurar variables de entorno
Write-Host ""
Write-Host "Configurando variables de entorno..." -ForegroundColor Yellow

[Environment]::SetEnvironmentVariable("FORTRESS_REPO", "$BasePath\fortress_v4", "User")
[Environment]::SetEnvironmentVariable("FORTRESS_RUNTIME", "$BasePath\fortress_runtime", "User")
[Environment]::SetEnvironmentVariable("FORTRESS_SECRETS", "$BasePath\fortress_secrets", "User")

Write-Host "  FORTRESS_REPO = $BasePath\fortress_v4" -ForegroundColor Gray
Write-Host "  FORTRESS_RUNTIME = $BasePath\fortress_runtime" -ForegroundColor Gray
Write-Host "  FORTRESS_SECRETS = $BasePath\fortress_secrets" -ForegroundColor Gray

# Verificar si estamos en el directorio del repo
$repoDir = "$BasePath\fortress_v4"
$currentDir = Get-Location

if ($currentDir.Path -ne $repoDir) {
    Write-Host ""
    Write-Host "ADVERTENCIA: No estás en el directorio del repositorio" -ForegroundColor Yellow
    Write-Host "Directorio actual: $($currentDir.Path)" -ForegroundColor Yellow
    Write-Host "Directorio esperado: $repoDir" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Por favor, copia los archivos del repo a: $repoDir" -ForegroundColor Yellow
}

# Crear .env.example en secrets
$envExample = "$BasePath\fortress_secrets\.env.example"
if (!(Test-Path $envExample)) {
    Write-Host ""
    Write-Host "Creando .env.example..." -ForegroundColor Yellow
    @"
# Coinbase API Credentials
COINBASE_KEY_NAME="organizations/your-org-id/apiKeys/your-key-id"
COINBASE_KEY_SECRET="-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----"
COINBASE_JWT_ISSUER="cdp"

# Opcional
LOG_LEVEL="INFO"
DRY_RUN="true"
"@ | Out-File -FilePath $envExample -Encoding UTF8
    Write-Host "  Creado: $envExample" -ForegroundColor Gray
}

# Resumen
Write-Host ""
Write-Host "Setup completado!" -ForegroundColor Green
Write-Host ""
Write-Host "Próximos pasos:" -ForegroundColor Cyan
Write-Host "  1. Copia los archivos del repo a: $repoDir" -ForegroundColor White
Write-Host "  2. Edita las credenciales en: $BasePath\fortress_secrets\.env" -ForegroundColor White
Write-Host "  3. Instala dependencias: pip install -e `\".\[dev\]\"" -ForegroundColor White
Write-Host "  4. Ejecuta tests: pytest tests/unit/ -v" -ForegroundColor White
Write-Host ""
Write-Host "Nota: Reinicia tu terminal para que las variables de entorno tengan efecto." -ForegroundColor Yellow
