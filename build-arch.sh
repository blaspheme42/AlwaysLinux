#!/bin/sh
# Construit alwayslinux-<version>-any.pkg.tar.zst dans ce dossier.
# Sur Arch : utilise makepkg directement. Ailleurs : dans un conteneur Arch via podman.
set -eu
cd "$(dirname "$0")"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
# makepkg attend les sources à côté du PKGBUILD
cp packaging/arch/PKGBUILD alwayslinux.py packaging/fr.blaspheme.AlwaysLinux.desktop packaging/fr.blaspheme.AlwaysLinux.svg LICENSE "$WORK/"

if command -v makepkg >/dev/null; then
  (cd "$WORK" && makepkg -f)
else
  # makepkg refuse de tourner en root : on construit avec un utilisateur jetable
  podman run --rm -v "$WORK:/build:Z" docker.io/archlinux/archlinux:latest sh -euc '
    pacman -Sy --noconfirm --needed base-devel python >/dev/null
    useradd -m builder && chown -R builder /build
    su builder -c "cd /build && makepkg -f --nodeps"
    chown -R root /build'
fi
cp "$WORK"/alwayslinux-*.pkg.tar.zst .
