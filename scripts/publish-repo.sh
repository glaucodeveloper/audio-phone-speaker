#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED_REPO="glaucodeveloper/audio-phone-speaker"
DEFAULT_BRANCH="main"
COMMIT_MESSAGE="${1:-Add Linux and Windows sender installation scripts}"

die() {
  echo "Erro: $*" >&2
  exit 1
}

command -v git >/dev/null 2>&1 || die "git não encontrado."

REPO_DIR="$(git rev-parse --show-toplevel 2>/dev/null || true)"
[[ -n "$REPO_DIR" ]] || die "execute este script dentro do repositório."

cd "$REPO_DIR"

ORIGIN_URL="$(git remote get-url origin 2>/dev/null || true)"
[[ -n "$ORIGIN_URL" ]] || die "remote 'origin' não configurado."

case "$ORIGIN_URL" in
  "https://github.com/$EXPECTED_REPO.git"|"https://github.com/$EXPECTED_REPO"|"git@github.com:$EXPECTED_REPO.git")
    ;;
  *)
    die "origin inesperado: $ORIGIN_URL"
    ;;
esac

CURRENT_BRANCH="$(git branch --show-current)"
[[ -n "$CURRENT_BRANCH" ]] || die "HEAD destacado; entre em uma branch antes de publicar."

if [[ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" ]]; then
  echo "Aviso: branch atual é '$CURRENT_BRANCH'; esperado: '$DEFAULT_BRANCH'."
fi

REQUIRED_FILES=(
  "audio_sender.py"
  "requirements-audio-sender.txt"
  "scripts/linux/run-sender.sh"
  "scripts/linux/install-service.sh"
  "scripts/linux/uninstall-service.sh"
  "scripts/windows/run-sender.ps1"
  "scripts/windows/install-autostart.ps1"
  "scripts/windows/uninstall-autostart.ps1"
  "docs/autostart.md"
)

for file in "${REQUIRED_FILES[@]}"; do
  [[ -f "$file" ]] || die "arquivo obrigatório ausente: $file"
done

chmod +x \
  scripts/linux/run-sender.sh \
  scripts/linux/install-service.sh \
  scripts/linux/uninstall-service.sh

# Inclui as alterações do sender, documentação, instaladores Linux/Windows
# e este próprio script, caso esteja salvo dentro de scripts/.
PATHS_TO_ADD=(
  ".gitignore"
  "README.md"
  "audio_sender.py"
  "requirements-audio-sender.txt"
  "docs"
  "scripts"
)

echo "Repositório: $REPO_DIR"
echo "Origin:      $ORIGIN_URL"
echo "Branch:      $CURRENT_BRANCH"
echo

git status --short

git add -- "${PATHS_TO_ADD[@]}"
git diff --cached --check

if git diff --cached --quiet; then
  echo
  echo "Nenhuma alteração nova para commit."
else
  echo
  echo "Arquivos preparados:"
  git diff --cached --name-status

  git commit -m "$COMMIT_MESSAGE"
fi

echo
echo "Atualizando referências remotas..."
git fetch origin "$CURRENT_BRANCH"

if ! git merge-base --is-ancestor "origin/$CURRENT_BRANCH" HEAD; then
  echo "A branch remota possui commits que ainda não estão localmente."
  echo "Aplicando rebase antes do push..."
  git rebase "origin/$CURRENT_BRANCH"
fi

echo
echo "Enviando para GitHub..."
git push -u origin "$CURRENT_BRANCH"

echo
echo "Publicado com sucesso."
git --no-pager log -1 --oneline
