#!/usr/bin/env python3
"""Publish new Flatpak builds of the apps in apps.json to apps.kznjk.com.

Each app's own CI attaches an unsigned build to its GitHub release:

  flatpak-build.tar   the `flatpak-builder --repo` directory, tarred
  flatpak-build.json  {"app_id", "branch", "tar_sha256"}, uploaded last

This is the only thing that holds the signing key. It starts from the repo as
last published (the newest publish-* release here, or a mirror of the live
repo on the first run), commits every build that isn't in it yet, signs the
result and writes flatpak-repo.tar + flatpak-repo.json for a new release. The
server applies that release only on top of the summary it was built on.

Publishes go one at a time: until the server is serving the previous one there
is nothing safe to build on, so this waits for it.

  ./publish.py check [--rebase-on-live]        # is there anything to publish?
  ./publish.py publish OUT [--rebase-on-live]  # build the signed repo into OUT

--rebase-on-live (or REBASE_ON_LIVE=true) builds on a mirror of the live repo
instead of the last publish, and republishes every app. It's the way out after
the server refused a publish because its repo had changed underneath it.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
PUBLISHER = os.environ.get("GITHUB_REPOSITORY", "steeb-k/kznjk-flatpak")
TOKEN = os.environ.get("GITHUB_TOKEN")
REPO_URL = os.environ.get("REPO_URL", "https://apps.kznjk.com/repo")
KEY_ID = os.environ.get("GPG_KEY_ID", "D6A05D0395B0401C5134203655F485F01EDE0F50")
PUBLIC_KEY = Path(os.environ.get("PUBLIC_KEY", HERE / "kznjk.gpg"))
TITLE = "apps.kznjk.com"
# Commits kept per ref, so a user can still roll back a few releases.
HISTORY = 5
# How long the server gets to apply a publish before this reports it stuck.
APPLY_TIMEOUT = timedelta(hours=1)


def log(message):
    print(message, flush=True)


def output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")


def http_get(url, dest=None, api=False):
    headers = {"User-Agent": "kznjk-flatpak-publish"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        if TOKEN:
            headers["Authorization"] = f"Bearer {TOKEN}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=300) as response:
        if dest is None:
            return response.read()
        with open(dest, "wb") as f:
            shutil.copyfileobj(response, f)


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def releases(repo):
    """Newest first, drafts left out."""
    found = json.loads(http_get(f"{API}/repos/{repo}/releases?per_page=30", api=True))
    return [r for r in found if not r["draft"]]


def assets(release):
    return {a["name"]: a["browser_download_url"] for a in release["assets"]}


def latest_builds(apps):
    """The newest build of each app on each branch, keyed "app_id//branch"."""
    builds = {}
    for app in apps:
        for release in releases(app["repo"]):
            files = assets(release)
            if not {"flatpak-build.json", "flatpak-build.tar"} <= files.keys():
                continue
            meta = json.loads(http_get(files["flatpak-build.json"]))
            where = f"{app['repo']} {release['tag_name']}"
            if meta["app_id"] != app["app_id"]:
                raise SystemExit(f"{where} is a build of {meta['app_id']}, not {app['app_id']}.")
            if not re.fullmatch(r"[A-Za-z0-9._-]+", meta["branch"]):
                raise SystemExit(f"{where} names an invalid branch {meta['branch']!r}.")
            builds.setdefault(f"{meta['app_id']}//{meta['branch']}", {
                "repo": app["repo"],
                "release": release["tag_name"],
                "app_id": meta["app_id"],
                "branch": meta["branch"],
                "tar_sha256": meta["tar_sha256"],
                "tar_url": files["flatpak-build.tar"],
            })
    return builds


def last_publish():
    for release in releases(PUBLISHER):
        files = assets(release)
        if release["tag_name"].startswith("publish-") and \
                {"flatpak-repo.json", "flatpak-repo.tar"} <= files.keys():
            return release, json.loads(http_get(files["flatpak-repo.json"])), files["flatpak-repo.tar"]
    return None, None, None


def live_summary():
    try:
        return hashlib.sha256(http_get(f"{REPO_URL}/summary")).hexdigest()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "none"
        raise


def plan(rebase_on_live):
    """Work out what to publish, and on top of what.

    Returns (pending builds, apps already published, live summary, last
    publish or None to build on a mirror of the live repo).
    """
    apps = json.loads((HERE / "apps.json").read_text())
    builds = latest_builds(apps)
    release, state, tar_url = last_publish()
    published = dict(state["apps"]) if state else {}
    live = live_summary()

    if rebase_on_live:
        return builds, published, live, None

    pending = {key: build for key, build in builds.items()
               if published.get(key, {}).get("tar_sha256") != build["tar_sha256"]}
    if state is None or not pending:
        return pending, published, live, None

    if live != state["summary"]:
        created = datetime.fromisoformat(release["created_at"].replace("Z", "+00:00"))
        waited = datetime.now(timezone.utc) - created
        message = (f"The server isn't serving {release['tag_name']} yet "
                   f"(summary {live}, expected {state['summary']}).")
        if waited > APPLY_TIMEOUT:
            raise SystemExit(f"{message} It's been {str(waited).split('.')[0]}: check flatpak-repo-sync on the "
                             "server, or run this workflow with rebase-on-live.")
        log(f"{message} Waiting for it before publishing "
            f"{', '.join(sorted(pending))}.")
        return {}, published, live, None
    return pending, published, live, (state, tar_url)


def check(args):
    pending, _, _, _ = plan(args.rebase_on_live)
    for key, build in sorted(pending.items()):
        log(f"To publish: {key} from {build['repo']} {build['release']}")
    if not pending:
        log("Nothing to publish.")
    output(publish=str(bool(pending)).lower())


def setup_gpg(work):
    """Import the signing key with its passphrase preset in the agent.

    ostree signs through gpgme, which can't be handed a passphrase, so the
    agent has to have it already, for every keygrip in case of subkeys.
    """
    home = work / "gnupg"
    home.mkdir(mode=0o700)
    (home / "gpg-agent.conf").write_text("allow-preset-passphrase\n")
    os.environ["GNUPGHOME"] = str(home)
    run("gpg", "--batch", "--quiet", "--import", input=os.environ["GPG_PRIVATE_KEY"].encode())
    listing = run("gpg", "--batch", "--with-colons", "--with-keygrip", "--list-secret-keys",
                  KEY_ID, capture_output=True, text=True).stdout
    libexec = run("gpgconf", "--list-dirs", "libexecdir", capture_output=True, text=True).stdout.strip()
    for line in listing.splitlines():
        fields = line.split(":")
        if fields[0] == "grp":
            run(f"{libexec}/gpg-preset-passphrase", "--preset", fields[9],
                input=os.environ["GPG_PASSPHRASE"].encode())


def app_refs(build_repo, build):
    """The refs a build may publish: its app and locale, on its own branch."""
    app_id, branch = build["app_id"], build["branch"]
    allowed, refs = [], run("ostree", "refs", f"--repo={build_repo}",
                            capture_output=True, text=True).stdout.split()
    for ref in refs:
        kind, name, _, ref_branch = (ref.split("/") + [""] * 4)[:4]
        if kind not in ("app", "runtime"):
            continue  # appstream: rebuilt by build-update-repo
        if name == f"{app_id}.Debug":
            continue  # most of the repo's size, and nobody installs it
        if ref_branch != branch or (kind, name) not in (("app", app_id),
                                                         ("runtime", f"{app_id}.Locale")):
            raise SystemExit(f"{build['repo']} {build['release']} carries {ref}, which "
                             f"isn't {app_id}'s to publish on {branch}.")
        allowed.append(ref)
    if not any(r.startswith("app/") for r in allowed):
        raise SystemExit(f"{build['repo']} {build['release']} has no app ref.")
    return allowed


def verify(work, repo, refs):
    """Check the repo the way a client would, against the committed public key."""
    env = dict(os.environ, FLATPAK_USER_DIR=str(work / "flatpak"))
    run("flatpak", "remote-add", "--user", f"--gpg-import={PUBLIC_KEY}", "check", f"file://{repo}", env=env)
    listed = run("flatpak", "remote-ls", "--user", "--all", "--columns=ref", "check",
                 env=env, capture_output=True, text=True).stdout.split()
    missing = [r for r in refs if r not in listed]
    if missing:
        raise SystemExit(f"The signed summary is missing {', '.join(missing)}.")

    client = work / "client"
    run("ostree", "init", f"--repo={client}", "--mode=bare-user")
    run("ostree", "remote", "add", f"--repo={client}", f"--gpg-import={PUBLIC_KEY}", "check", f"file://{repo}")
    for ref in refs:
        run("ostree", "pull", f"--repo={client}", "--commit-metadata-only", "check", ref)


def publish(args):
    pending, published, live, base = plan(args.rebase_on_live)
    if not pending:
        log("Nothing to publish.")
        output(publish="false")
        return

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        repo = work / "repo"
        repo.mkdir()

        if base:
            state, tar_url = base
            log(f"Building on the last publish, summary {state['summary']}.")
            http_get(tar_url, work / "base.tar")
            if sha256(work / "base.tar") != state["tar_sha256"]:
                raise SystemExit("The last publish's flatpak-repo.tar doesn't match its checksum.")
            run("tar", "-C", repo, "-xf", work / "base.tar")
            (work / "base.tar").unlink()
            (repo / "tmp").mkdir(exist_ok=True)
        else:
            run("ostree", "init", f"--repo={repo}", "--mode=archive-z2")
            if live != "none":
                log(f"Building on a mirror of {REPO_URL}, summary {live}.")
                run("ostree", "remote", "add", f"--repo={repo}", f"--gpg-import={PUBLIC_KEY}",
                    "--set=gpg-verify-summary=true", "live", REPO_URL)
                run("ostree", "pull", f"--repo={repo}", "--mirror", "--depth=-1", "live")
                run("ostree", "remote", "delete", f"--repo={repo}", "live")
            else:
                log(f"No repo at {REPO_URL} yet, starting a new one.")

        setup_gpg(work)
        committed, notes = [], []
        try:
            for key, build in sorted(pending.items()):
                log(f"Committing {key} from {build['repo']} {build['release']}")
                build_repo = work / "build"
                build_tar = work / "build.tar"
                http_get(build["tar_url"], build_tar)
                if sha256(build_tar) != build["tar_sha256"]:
                    raise SystemExit(f"{build['repo']} {build['release']}: flatpak-build.tar "
                                     "doesn't match flatpak-build.json.")
                build_repo.mkdir()
                run("tar", "-C", build_repo, "-xf", build_tar)
                build_tar.unlink()
                refs = app_refs(build_repo, build)
                # Stamped now, not with the build's own time: clients refuse an
                # update that's older than what they have, and a re-released
                # older build would be.
                run("flatpak", "build-commit-from", f"--src-repo={build_repo}",
                    f"--gpg-sign={KEY_ID}", "--no-update-summary", "--timestamp=NOW",
                    f"--subject={build['repo']} {build['release']}", repo, *refs)
                shutil.rmtree(build_repo)
                committed += refs
                published[key] = {k: build[k] for k in ("repo", "release", "tar_sha256")}
                notes.append(f"- {key}: [{build['repo']} {build['release']}]"
                             f"(https://github.com/{build['repo']}/releases/tag/{build['release']})")

            run("flatpak", "build-update-repo", f"--gpg-sign={KEY_ID}", f"--title={TITLE}",
                "--generate-static-deltas", "--prune", f"--prune-depth={HISTORY}", repo)
        finally:
            subprocess.run(["gpgconf", "--kill", "gpg-agent"])

        verify(work, repo, committed)

        run("tar", "-C", repo, "--exclude=./tmp", "--exclude=./.lock", "-cf", out / "flatpak-repo.tar", ".")
        state = {
            "base_summary": live,
            "summary": sha256(repo / "summary"),
            "tar_sha256": sha256(out / "flatpak-repo.tar"),
            "apps": published,
        }
    (out / "flatpak-repo.json").write_text(json.dumps(state, indent=2) + "\n")
    (out / "notes.md").write_text("Published:\n\n" + "\n".join(notes) + "\n")
    log(json.dumps(state, indent=2))
    output(publish="true", tag=datetime.now(timezone.utc).strftime("publish-%Y%m%d-%H%M%S"))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "publish"):
        command = sub.add_parser(name)
        command.add_argument("--rebase-on-live", action="store_true",
                             default=os.environ.get("REBASE_ON_LIVE") == "true")
        if name == "publish":
            command.add_argument("out", type=Path)
    args = parser.parse_args()
    (check if args.command == "check" else publish)(args)


if __name__ == "__main__":
    sys.exit(main())
