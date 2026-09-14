# kznjk-flatpak

Publishes the Flatpak apps at [apps.kznjk.com](https://apps.kznjk.com). This
repository is the only thing that signs them.

```sh
flatpak remote-add --if-not-exists kznjk https://apps.kznjk.com/kznjk.flatpakrepo
```

## How a release gets out

1. An app's own CI builds it and attaches two files to the app's GitHub release:
   `flatpak-build.tar` (the `flatpak-builder --repo` directory, tarred) and then
   `flatpak-build.json`.
2. Every 15 minutes, [Publish](.github/workflows/publish.yml) checks each app in
   [`apps.json`](apps.json) for a build it hasn't published. If there is one, it
   takes the repo as last published, commits the new builds on top, signs
   everything, verifies it against the pinned [`kznjk.gpg`](kznjk.gpg) and
   attaches the signed repo to a `publish-*` release here.
3. The server behind apps.kznjk.com polls for that release (outbound only; it
   accepts no connections from CI), checks it, and applies it.

Publishes go one at a time. A new one is only built once the server is serving
the previous one, so two apps releasing together just land one after the other.
If the server hasn't caught up after an hour, the check fails.

## Adding an app

Add it to `apps.json`:

```json
{"repo": "steeb-k/example", "app_id": "io.github.steeb_k.Example"}
```

Then have its release workflow build with `flatpak-builder --repo=repo
--default-branch=stable` (the `flatpak/flatpak-github-actions/flatpak-builder`
action with `branch: stable` does this) and attach the result:

```sh
tar -C repo -cf flatpak-build.tar .
sha=$(sha256sum flatpak-build.tar | cut -d' ' -f1)
printf '{"app_id": "io.github.steeb_k.Example", "branch": "stable", "tar_sha256": "%s"}\n' "$sha" > flatpak-build.json
gh release upload "$TAG" --clobber flatpak-build.tar
gh release upload "$TAG" --clobber flatpak-build.json   # last
```

A build may only carry its own app ref and its `.Locale` on the branch it
names. Anything else is refused, `.Debug` is dropped, and appstream is rebuilt
here. Each branch gets the newest release that has a build attached,
pre-releases included, so a beta channel is just `"branch": "beta"`.

## Secrets

The `flatpak-repo` environment, usable from `main` only, holds
`FLATPAK_GPG_PRIVATE_KEY` (armored) and `FLATPAK_GPG_PASSPHRASE` for key
`D6A0 5D03 95B0 401C 5134  2036 55F4 85F0 1EDE 0F50`.

## When the server refuses a publish

The server applies a publish only on top of the exact repo it was built on. If
its repo changed some other way, it logs a refusal and the next check here fails
once the hour is up. Run **Publish** by hand with **rebase-on-live**: it builds
on a mirror of what the server actually serves and republishes every app.
