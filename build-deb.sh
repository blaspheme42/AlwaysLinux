#!/bin/sh
# Construit alwayslinux_<version>_all.deb dans ce dossier.
set -eu
cd "$(dirname "$0")"
VERSION=0.1.0
ID=fr.blaspheme.AlwaysLinux
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
chmod 755 "$ROOT"  # mktemp crée en 700 : le paquet décrirait / comme privé

python3 alwayslinux.py --test

install -Dm755 alwayslinux.py "$ROOT/usr/bin/alwayslinux"
install -Dm644 packaging/$ID.desktop "$ROOT/usr/share/applications/$ID.desktop"
install -Dm644 packaging/$ID.svg "$ROOT/usr/share/icons/hicolor/scalable/apps/$ID.svg"
install -Dm644 LICENSE "$ROOT/usr/share/doc/alwayslinux/copyright"
mkdir -p "$ROOT/DEBIAN"
cat > "$ROOT/DEBIAN/control" <<EOF
Package: alwayslinux
Version: $VERSION
Architecture: all
Maintainer: Benjamin Marco <154837737+blaspheme42@users.noreply.github.com>
Depends: python3 (>= 3.8), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1
Recommends: appstream, flatpak
Section: admin
Priority: optional
Homepage: https://github.com/blaspheme42/AlwaysLinux
Description: trouve la commande pour installer une appli sur Linux
 Tape le nom d'une appli : AlwaysLinux donne la commande d'installation
 pour ta distribution (apt, dnf, pacman...) et pour Flatpak, avec les logos.
EOF

dpkg-deb --root-owner-group --build "$ROOT" "alwayslinux_${VERSION}_all.deb"
