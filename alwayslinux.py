#!/usr/bin/env python3
"""AlwaysLinux : trouve la commande d'installation d'une appli selon ta distribution.

Pour ta propre distribution (si elle utilise apt), tout est lu en local : catalogue AppStream,
apt-cache et flatpak. Pour les autres distributions, on interroge repology.org (avec un cache disque).
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

API = "https://repology.org/api/v1"
FLATHUB = "https://flathub.org/api/v2/search"
UA = "alwayslinux/0.1 (projet perso)"  # Repology demande un User-Agent identifiable
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "alwayslinux")
ARCH = os.uname().machine

# (nom affiché, identifiant du dépôt chez Repology, modèle de commande)
DISTROS = [
    ("Debian 12", "debian_12", "sudo apt install {}"),
    ("Debian 13", "debian_13", "sudo apt install {}"),
    ("Ubuntu 24.04", "ubuntu_24_04", "sudo apt install {}"),
    ("Fedora 42", "fedora_42", "sudo dnf install {}"),
    ("Arch Linux", "arch", "sudo pacman -S {}"),
    ("openSUSE Tumbleweed", "opensuse_tumbleweed", "sudo zypper install {}"),
    ("Alpine (edge)", "alpine_edge", "sudo apk add {}"),
    ("Void Linux", "void_x86_64", "sudo xbps-install {}"),
]
# Commande pour installer AppStream et son catalogue (noms vérifiés sur repology.org)
APPSTREAM = {
    "debian_12": "sudo apt install appstream && sudo apt update",
    "debian_13": "sudo apt install appstream && sudo apt update",
    "ubuntu_24_04": "sudo apt install appstream && sudo apt update",
    "fedora_42": "sudo dnf install appstream appstream-data",
    "arch": "sudo pacman -S appstream archlinux-appstream-data",
    "opensuse_tumbleweed": "sudo zypper install AppStream",
    "alpine_edge": "sudo apk add appstream",
    "void_x86_64": "sudo xbps-install AppStream",
}
APP_KINDS = ("desktop-application", "console-application")

# Terminal → arguments placés avant la commande à exécuter (premier trouvé = utilisé)
TERMINALS = [
    ("gnome-terminal", ["--"]), ("kgx", ["--"]), ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]),
    ("alacritty", ["-e"]), ("kitty", []), ("foot", []), ("xterm", ["-e"]), ("x-terminal-emulator", ["-e"]),
]
# Affiche la commande déjà tapée et modifiable : rien ne s'exécute avant que l'utilisateur appuie sur Entrée.
# La commande arrive en $1 (jamais collée dans le script), puis on rend la main au shell habituel.
PREFILL = ('echo "Entrée pour lancer, Ctrl+C pour annuler."; '
           'read -e -i "$1" -p "$ " cmd && eval "$cmd"; exec "${SHELL:-bash}"')


def open_terminal(cmd):
    """Ouvre un terminal avec la commande préremplie. False si aucun terminal n'est trouvé."""
    for name, args in TERMINALS:
        if shutil.which(name):
            try:
                subprocess.Popen([name, *args, "bash", "-c", PREFILL, "alwayslinux", cmd], start_new_session=True)
                return True
            except OSError:
                continue
    return False


def detect(os_release):
    """Index dans DISTROS de la distribution décrite par le contenu de /etc/os-release."""
    info = dict(l.split("=", 1) for l in os_release.splitlines() if "=" in l)
    info = {k: v.strip().strip('"') for k, v in info.items()}
    ids = [info.get("ID", "")] + info.get("ID_LIKE", "").split()
    exact = f"{ids[0]}_{info.get('VERSION_ID', '').replace('.', '_')}"
    for i, (_, repo, _) in enumerate(DISTROS):
        if repo in (exact, ids[0]):
            return i
    # Version inconnue ou distribution dérivée (Mint → Ubuntu, Manjaro → Arch…)
    for x in filter(None, ids):
        for i, (_, repo, _) in enumerate(DISTROS):
            if repo.startswith(x):
                return i
    return 0


def run(*cmd):
    """Sortie d'une commande en anglais (format stable à analyser), "" si elle échoue."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                              env={**os.environ, "LC_ALL": "C.UTF-8"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# --- Sources locales -------------------------------------------------------------------------

def parse_appstream(text):
    """Composants de la sortie de `appstreamcli search` (blocs « Clé: valeur » séparés par ---)."""
    comps = []
    for block in text.split("\n---"):
        comp, key = {}, None
        for line in block.splitlines():
            if line.startswith("  ") and key:
                comp[key] = (comp[key] + " " + line.strip()).strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                comp[key] = value.strip()
        if "Identifier" in comp:
            comp["kind"] = comp["Identifier"].rpartition("[")[2].rstrip("]")
            comps.append(comp)
    return comps


def parse_apt_policy(text):
    """{paquet: (version candidate, installé ?)} pour les paquets disponibles."""
    result, pkg, installed = {}, None, None
    for line in text.splitlines():
        if not line.startswith(" ") and line.endswith(":"):
            pkg = line[:-1]
        elif line.strip().startswith("Installed:"):
            installed = line.split(":", 1)[1].strip()
        elif line.strip().startswith("Candidate:") and pkg:
            candidate = line.split(":", 1)[1].strip()
            if candidate != "(none)":
                result[pkg] = (candidate, installed != "(none)")
    return result


def local_icon(icon, flatpak):
    """("file", chemin) ou ("name", icône du thème) pour une icône AppStream, None sinon."""
    if not icon:
        return None
    if not icon.endswith(".png"):
        return ("name", icon)
    if flatpak:
        dirs = [f"{root}/appstream/*/{ARCH}/active/icons/128x128"
                for root in ("/var/lib/flatpak", os.path.expanduser("~/.local/share/flatpak"))]
    else:  # Debian 12+ : swcatalog ; Ubuntu plus ancien : app-info
        dirs = ["/var/lib/swcatalog/icons/*/64x64", "/var/lib/app-info/icons/*/64x64"]
    for d in dirs:
        found = glob.glob(os.path.join(d, icon))
        if found:
            return ("file", found[0])
    return None


def split_components(comps):
    """(applis des dépôts, applis Flatpak) depuis les composants AppStream, sans doublons."""
    debs, flats, seen = [], [], set()
    for c in comps:
        if c["kind"] not in APP_KINDS:
            continue
        item = {"name": c.get("Name", ""), "summary": c.get("Summary", "")}
        if c.get("Package") and c["Package"] not in seen:
            seen.add(c["Package"])
            debs.append({**item, "pkg": c["Package"], "icon": local_icon(c.get("Icon"), False)})
        elif c.get("Bundle", "").startswith("flatpak:app/"):
            app_id = c["Bundle"].split("/")[1]
            if app_id not in seen:
                seen.add(app_id)
                flats.append({**item, "app_id": app_id, "icon": local_icon(c.get("Icon"), True)})
    return debs, flats


def local_lookup(query):
    """Recherche entièrement locale (distribution apt + flatpak)."""
    debs, flats = split_components(parse_appstream(run("appstreamcli", "search", query)))
    # Les outils en ligne de commande (htop, git…) n'ont souvent pas de fiche AppStream
    if query not in {d["pkg"] for d in debs}:
        debs.insert(0, {"name": query, "summary": "", "pkg": query, "icon": ("name", query)})  # logo du thème si installé
    status = parse_apt_policy(run("apt-cache", "policy", *[d["pkg"] for d in debs]))
    debs = [{**d, "version": status[d["pkg"]][0], "installed": status[d["pkg"]][1]}
            for d in debs if d["pkg"] in status][:5]
    return {"debs": debs, "flatpaks": with_flatpak_status(flats[:3])}


def with_flatpak_status(flats):
    installed = set(run("flatpak", "list", "--app", "--columns=application").split())
    return [{**f, "installed": f["app_id"] in installed} for f in flats]


# --- Sources en ligne (avec cache disque) ----------------------------------------------------

def cached(name, max_age, compute):
    """Valeur JSON gardée dans ~/.cache/alwayslinux/ pendant max_age secondes."""
    path = os.path.join(CACHE, name)
    try:
        if time.time() - os.path.getmtime(path) < max_age:
            with open(path) as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    value = compute()
    os.makedirs(CACHE, exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(value, f)
    os.replace(path + ".tmp", path)  # pas de fichier à moitié écrit si l'appli est coupée
    return value


def fetch(url, body=None, raw=False):
    headers = {"User-Agent": UA, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=15) as r:
        return r.read() if raw else json.load(r)


def name_of(pkg):
    return pkg.get("binname") or pkg.get("srcname") or pkg.get("visiblename")


def pick(packages, repo, query):
    """Le paquet à installer pour ce dépôt : nom exact si possible, sinon le nom le plus court."""
    pkgs = [p for p in packages if p["repo"] == repo]
    if not pkgs:
        return None
    return min(pkgs, key=lambda p: (name_of(p) != query, len(name_of(p))))


def repology(query):
    """(paquets du projet, suggestions si le projet n'existe pas), en cache 7 jours."""
    def compute():
        q = urllib.parse.quote(query)
        keep = ("repo", "binname", "srcname", "visiblename", "version")  # allège le cache
        pkgs = [{k: p[k] for k in keep if k in p} for p in fetch(f"{API}/project/{q}")]
        suggestions = [] if pkgs else sorted(fetch(f"{API}/projects/?search={q}"), key=len)[:12]
        return [pkgs, suggestions]
    return cached("repology-" + urllib.parse.quote(query, safe="") + ".json", 7 * 86400, compute)


def flatpak_lookup(query):
    """Applis Flatpak : catalogue local si flatpak est installé, sinon l'API Flathub."""
    if shutil.which("flatpak") and shutil.which("appstreamcli"):
        return with_flatpak_status(split_components(parse_appstream(run("appstreamcli", "search", query)))[1][:3])
    try:
        hits = cached("flathub-" + urllib.parse.quote(query, safe="") + ".json", 86400,
                      lambda: fetch(FLATHUB, {"query": query})["hits"][:3])
    except Exception:  # Flathub en panne ne doit pas cacher le résultat Repology
        return []
    return [{"name": h["name"], "summary": h.get("summary") or "", "app_id": h["app_id"],
             "icon": ("url", h["icon"]) if h.get("icon") else None, "installed": False} for h in hits]


def icon_file(url):
    """Chemin local d'un logo téléchargé (gardé dans le cache)."""
    path = os.path.join(CACHE, "icons", hashlib.sha1(url.encode()).hexdigest() + ".png")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(fetch(url, raw=True))
        os.replace(path + ".tmp", path)
    return path


def self_check():
    pkgs = [
        {"repo": "arch", "srcname": "vlc", "binname": "vlc-plugin-x264"},
        {"repo": "arch", "srcname": "vlc", "binname": "vlc"},
        {"repo": "debian_12", "srcname": "firefox-esr", "binname": "firefox-esr-l10n-fr"},
        {"repo": "debian_12", "srcname": "firefox-esr", "binname": "firefox-esr"},
    ]
    assert name_of(pick(pkgs, "arch", "vlc")) == "vlc"
    assert name_of(pick(pkgs, "debian_12", "firefox")) == "firefox-esr"
    assert pick(pkgs, "fedora_42", "vlc") is None
    assert all(repo in APPSTREAM for _, repo, _ in DISTROS)
    labels = [d[0] for d in DISTROS]
    assert labels[detect('ID=debian\nVERSION_ID="12"')] == "Debian 12"
    assert labels[detect('ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="22"')] == "Ubuntu 24.04"
    assert labels[detect("ID=manjaro\nID_LIKE=arch")] == "Arch Linux"
    assert labels[detect('ID=fedora\nVERSION_ID=44')] == "Fedora 42"

    comps = parse_appstream(
        "Identifier: org.videolan.vlc [desktop-application]\nName: VLC\nSummary: Media player\n"
        "Package: vlc\nIcon: vlc_vlc.png\n---\n"
        "Identifier: org.videolan.VLC.Plugin.bdj [addon]\nName: BDJ\n"
        "Bundle: flatpak:runtime/org.videolan.VLC.Plugin.bdj/x86_64/3-25.08\n---\n"
        "Identifier: com.discordapp.Discord [desktop-application]\nName: Discord\nSummary: \n"
        "  Talk, play,\n  hang out\nBundle: flatpak:app/com.discordapp.Discord/x86_64/stable\n")
    assert [c["kind"] for c in comps] == ["desktop-application", "addon", "desktop-application"]
    assert comps[2]["Summary"] == "Talk, play, hang out"
    debs, flats = split_components(comps)
    assert [d["pkg"] for d in debs] == ["vlc"] and [f["app_id"] for f in flats] == ["com.discordapp.Discord"]

    policy = parse_apt_policy("htop:\n  Installed: 3.2.2-2\n  Candidate: 3.2.2-2\n"
                              "gimp:\n  Installed: (none)\n  Candidate: 2.10.34-1\n"
                              "nope:\n  Installed: (none)\n  Candidate: (none)\n")
    assert policy == {"htop": ("3.2.2-2", True), "gimp": ("2.10.34-1", False)}
    print("self-check ok")


def main():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, GLib, Gtk

    try:
        with open("/etc/os-release") as f:
            home = detect(f.read())
    except OSError:
        home = 0
    # Lecture locale seulement pour la distribution de la machine, et si elle utilise apt
    has_apt = bool(shutil.which("apt-cache"))

    class Window(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="AlwaysLinux", default_width=600, default_height=680)
            self.cache = {}  # (requête, source) → résultat
            self.pending = set()
            self.current = None

            self.distro = Gtk.DropDown.new_from_strings([d[0] for d in DISTROS])
            self.distro.set_selected(home)
            self.distro.connect("notify::selected", lambda *_: self.render())

            self.entry = Gtk.SearchEntry(placeholder_text="Nom de l'appli (ex : vlc, gimp, firefox) puis Entrée", hexpand=True)
            self.entry.connect("activate", lambda e: self.search(e.get_text()))

            self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            self.toasts = Adw.ToastOverlay()

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                          margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
            box.append(self.distro)
            box.append(self.entry)
            self.scroll = Gtk.ScrolledWindow(child=self.results, vexpand=True)
            box.append(self.scroll)
            self.toasts.set_child(box)

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root.append(Adw.HeaderBar())
            root.append(self.toasts)
            self.set_content(root)
            self.show(*self.appstream_tip(), self.status("system-search-symbolic", "Cherche une appli",
                                                         "Tape son nom puis Entrée pour obtenir la commande d'installation."))

        def appstream_tip(self):
            """Propose d'installer AppStream s'il manque (vérifié à chaque fois : pas besoin de relancer)."""
            if shutil.which("appstreamcli"):
                return []
            return [self.boxed(self.cmd_row(("name", "dialog-information-symbolic"), "Active la recherche locale",
                                            "AppStream n'est pas installé : installe-le pour des résultats "
                                            "plus rapides, hors ligne et avec les logos.",
                                            APPSTREAM[DISTROS[home][1]], False))]

        def key(self):
            local = has_apt and shutil.which("appstreamcli") and self.distro.get_selected() == home
            return (self.current, "local" if local else "remote")

        def search(self, text):
            query = text.strip().lower()
            if query:
                self.current = query
                self.render()

        def fetch(self, key):
            if key in self.pending:
                return
            self.pending.add(key)
            self.show(Gtk.Spinner(spinning=True, halign=Gtk.Align.START))
            query, source = key

            def work():
                try:
                    if source == "local":
                        result = local_lookup(query)
                    else:
                        pkgs, suggestions = repology(query)
                        result = {"pkgs": pkgs, "suggestions": suggestions, "flatpaks": flatpak_lookup(query)}
                except Exception as e:  # réseau coupé, API indisponible…
                    result = e
                GLib.idle_add(self.done, key, result)

            threading.Thread(target=work, daemon=True).start()

        def done(self, key, result):
            self.pending.discard(key)
            if isinstance(result, Exception):
                if key == self.key():
                    self.show(self.status("network-offline-symbolic", "Impossible de joindre repology.org", str(result)))
                return
            self.cache[key] = result
            if key == self.key():
                self.render()

        def render(self):
            if not self.current:
                return
            key = self.key()
            if key not in self.cache:
                return self.fetch(key)
            result = self.cache[key]
            label, repo, template = DISTROS[self.distro.get_selected()]
            widgets = self.appstream_tip() + [self.label(f"Dépôts de {label}", ["heading"])]

            if key[1] == "local":
                if result["debs"]:
                    widgets.append(self.boxed(*[
                        self.cmd_row(d["icon"], d["name"], d["summary"] or f"Version {d['version']}",
                                     template.format(d["pkg"]), d["installed"]) for d in result["debs"]]))
                else:
                    widgets.append(self.boxed(self.info_row(f"Rien trouvé pour « {self.current} » dans les dépôts.")))
            else:
                widgets += self.remote_widgets(result, label, repo, template)

            widgets.append(self.label("Via Flatpak (Flathub)", ["heading"]))
            rows = [self.cmd_row(f["icon"], f["name"], f["summary"], f"flatpak install flathub {f['app_id']}",
                                 f["installed"]) for f in result["flatpaks"]]
            widgets.append(self.boxed(*rows) if rows else self.label("Rien trouvé sur Flathub.", ["dim-label"]))
            self.show(*widgets)

        def remote_widgets(self, result, label, repo, template):
            pkgs, suggestions = result["pkgs"], result["suggestions"]
            pkg = pick(pkgs, repo, self.current)
            if pkg:
                return [self.boxed(self.cmd_row(None, name_of(pkg), f"Version {pkg.get('version', '?')}",
                                                template.format(name_of(pkg)), False))]
            if pkgs:
                names = [d[0] for d in DISTROS if d[1] in {p["repo"] for p in pkgs}]
                return [self.boxed(self.info_row(
                    f"« {self.current} » n'est pas dans les dépôts de {label}." +
                    (f" Disponible sur : {', '.join(names)}." if names else "")))]
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4)
            for name in suggestions:
                b = Gtk.Button(label=name, css_classes=["pill"])
                b.connect("clicked", lambda _, n=name: (self.entry.set_text(n), self.search(n)))
                flow.append(b)
            return [self.boxed(self.info_row(f"Aucun paquet nommé « {self.current} »." +
                                             (" Tu cherchais peut-être :" if suggestions else ""))), flow]

        def boxed(self, *rows):
            box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
            for r in rows:
                box.append(r)
            return box

        def cmd_row(self, icon, title, subtitle, cmd, installed):
            esc = GLib.markup_escape_text
            row = Adw.ActionRow(title=esc(title), subtitle=(esc(subtitle) + "\n" if subtitle else "") + f"<tt>{esc(cmd)}</tt>")
            row.add_prefix(self.image(icon))
            if installed:
                row.add_suffix(Gtk.Label(label="Installé", css_classes=["success", "caption-heading"]))
            copy = Gtk.Button(icon_name="utilities-terminal-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                              tooltip_text="Ouvrir dans le terminal (commande préremplie, non lancée) et copier")
            copy.connect("clicked", lambda _: self.copy(cmd))
            row.add_suffix(copy)
            row.set_activatable_widget(copy)  # cliquer n'importe où sur la ligne fait pareil que le bouton
            return row

        def image(self, icon):
            image = Gtk.Image(icon_name="package-x-generic", pixel_size=48)
            kind, value = icon or (None, None)
            if kind == "name" and Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).has_icon(value):
                image.set_from_icon_name(value)
            elif kind == "name" and os.path.exists(f"/usr/share/pixmaps/{value}.png"):  # applis installées hors thème (.deb)
                image.set_from_file(f"/usr/share/pixmaps/{value}.png")
            elif kind == "file":
                image.set_from_file(value)
            elif kind == "url":
                def work():
                    try:
                        path = icon_file(value)
                    except Exception:  # pas de logo : on garde l'icône générique
                        return
                    GLib.idle_add(image.set_from_file, path)
                threading.Thread(target=work, daemon=True).start()
            return image

        def info_row(self, text):
            row = Adw.ActionRow(title=GLib.markup_escape_text(text))
            row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
            return row

        def status(self, icon, title, description=""):
            return Adw.StatusPage(icon_name=icon, title=title, description=description, vexpand=True)

        def copy(self, text):
            Gdk.Display.get_default().get_clipboard().set(text)  # reste dispo si le terminal ne s'ouvre pas
            ok = open_terminal(text)
            self.toasts.add_toast(Adw.Toast(title="Commande prête dans le terminal" if ok else "Commande copiée"))

        def label(self, text, classes=()):
            return Gtk.Label(label=text, wrap=True, xalign=0, css_classes=list(classes))

        def show(self, *widgets):
            while child := self.results.get_first_child():
                self.results.remove(child)
            for w in widgets:
                self.results.append(w)
            self.scroll.get_vadjustment().set_value(0)

    app = Adw.Application(application_id="fr.blaspheme.AlwaysLinux")
    app.connect("activate", lambda a: Window(a).present())
    app.run([])


if __name__ == "__main__":
    self_check() if "--test" in sys.argv else main()
