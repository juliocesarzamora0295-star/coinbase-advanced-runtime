#!/bin/bash
# Fortress v4 - Setup Script (Linux/Mac)
# Ejecutar como: ./scripts/setup.sh

set -e

BASE_PATH="${HOME}/fortress"

# Colores
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[0;37m'
NC='\033[0m' # No Color

echo -e "${GREEN}Fortress v4 - Setup Script${NC}"
echo -e "${GREEN}==========================${NC}"
echo ""

# Verificar Python
if ! command -v python3 &> /dev/null; then
    echo -e "${YELLOW}Error: Python 3 no está instalado${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 --version)
echo -e "${CYAN}Python detectado: $PYTHON_VERSION${NC}"

# Crear directorios
echo ""
echo -e "${YELLOW}Creando directorios...${NC}"

mkdir -p "$BASE_PATH"/fortress_v4
mkdir -p "$BASE_PATH"/fortress_runtime/data/raw
mkdir -p "$BASE_PATH"/fortress_runtime/data/processed
mkdir -p "$BASE_PATH"/fortress_runtime/runs
mkdir -p "$BASE_PATH"/fortress_runtime/reports
mkdir -p "$BASE_PATH"/fortress_runtime/logs
mkdir -p "$BASE_PATH"/fortress_runtime/cache
mkdir -p "$BASE_PATH"/fortress_runtime/state
mkdir -p "$BASE_PATH"/fortress_secrets

echo -e "${GRAY}  Directorios creados en: $BASE_PATH${NC}"

# Configurar variables de entorno
echo ""
echo -e "${YELLOW}Configurando variables de entorno...${NC}"

SHELL_RC=""
if [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
fi

if [ -n "$SHELL_RC" ]; then
    # Eliminar entradas antiguas si existen
    sed -i '/# Fortress v4/d' "$SHELL_RC" 2>/dev/null || true
    sed -i '/FORTRESS_REPO/d' "$SHELL_RC" 2>/dev/null || true
    sed -i '/FORTRESS_RUNTIME/d' "$SHELL_RC" 2>/dev/null || true
    sed -i '/FORTRESS_SECRETS/d' "$SHELL_RC" 2>/dev/null || true
    
    # Agregar nuevas entradas
    echo "" >> "$SHELL_RC"
    echo "# Fortress v4" >> "$SHELL_RC"
    echo "export FORTRESS_REPO=\"$BASE_PATH/fortress_v4\"" >> "$SHELL_RC"
    echo "export FORTRESS_RUNTIME=\"$BASE_PATH/fortress_runtime\"" >> "$SHELL_RC"
    echo "export FORTRESS_SECRETS=\"$BASE_PATH/fortress_secrets\"" >> "$SHELL_RC"
    
    echo -e "${GRAY}  Variables agregadas a: $SHELL_RC${NC}"
else
    echo -e "${YELLOW}  No se encontró .bashrc o .zshrc${NC}"
    echo -e "${YELLOW}  Agrega manualmente:${NC}"
    echo "export FORTRESS_REPO=\"$BASE_PATH/fortress_v4\""
    echo "export FORTRESS_RUNTIME=\"$BASE_PATH/fortress_runtime\""
    echo "export FORTRESS_SECRETS=\"$BASE_PATH/fortress_secrets\""
fi

# Crear .env.example
ENV_EXAMPLE="$BASE_PATH/fortress_secrets/.env.example"
if [ ! -f "$ENV_EXAMPLE" ]; then
    echo ""
    echo -e "${YELLOW}Creando .env.example...${NC}"
    cat > "$ENV_EXAMPLE" << 'EOF'
# Coinbase API Credentials
COINBASE_KEY_NAME="organizations/your-org-id/apiKeys/your-key-id"
COINBASE_KEY_SECRET="-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----"
COINBASE_JWT_ISSUER="cdp"

# Opcional
LOG_LEVEL="INFO"
DRY_RUN="true"
EOF
    echo -e "${GRAY}  Creado: $ENV_EXAMPLE${NC}"
fi

# Resumen
echo ""
echo -e "${GREEN}Setup completado!${NC}"
echo ""
echo -e "${CYAN}Próximos pasos:${NC}"
echo -e "  ${WHITE}1. Copia los archivos del repo a: $BASE_PATH/fortress_v4${NC}"
echo -e "  ${WHITE}2. Edita las credenciales en: $BASE_PATH/fortress_secrets/.env${NC}"
echo -e "  ${WHITE}3. Recarga el shell: source $SHELL_RC${NC}"
echo -e "  ${WHITE}4. Instala dependencias: pip install -e '.[dev]'${NC}"
echo -e "  ${WHITE}5. Ejecuta tests: pytest tests/unit/ -v${NC}"
echo ""
echo -e "${YELLOW}Nota: Reinicia tu terminal o ejecuta 'source $SHELL_RC' para que las variables tengan efecto.${NC}"
