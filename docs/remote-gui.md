# Remote GUI over SSH

## What it is

With the [SSH service](installation.md#optional-ssh-service) a remote session
cannot open a window on the host's screen, so by default the GUI launchers
(`jb ide`, `jb chrome`, `jb firefox`, `jb browser`, `jb apps run`) are
host-only. With `remote.ssh.gui` on, they instead draw on a **shared RDP
display**: a small `jailbee-display` container running the weston compositor
(RDP backend, software rendering). You tunnel its port over the existing SSH
connection and view it in any RDP client.

There is one screen for every container. Apps from different containers appear
side by side on it.

## Turn it on

1. Set `remote.ssh.gui: true` in `global.yaml` (see
   [`remote.ssh`](config.md#remotessh)).
2. `jb display up`. The first run pulls an image and installs weston, which
   takes a few minutes; later runs are instant.

The flag is re-read for every new session and every forward request, so no
`jb remote ssh restart` is needed to turn it on or off. Containers mount the
display directory at their next start (or on the first GUI launch) only while
it is on; `jb display up` warns if it is still off.

`jb display up` prints the connection recipe; `jb display status` prints it
again.

## Connect

The forward is accepted only after your SSH key has launched a GUI app, so
launch first, then connect: the launch prints this recipe and waits up to 120
seconds for you (see [Launch](#launch)). On your own computer, in two steps:

```bash
ssh -N -L 3389:127.0.0.1:13389 -p <ssh port> jailbee@<host>
```

then point an RDP client at `localhost:3389`. Any client works: `mstsc` on
Windows, Windows App (formerly Microsoft Remote Desktop) on macOS, `xfreerdp` or
Remmina on Linux.
There is no username or password prompt; enter anything if the client insists.

Already in an SSH session? Add the forward to it with `~C`, then
`-L 3389:127.0.0.1:13389`.

The forward is authorized per SSH key: it works for a key only after that key
has launched a GUI app (the launch registers a grant), and only to
`127.0.0.1:13389`.

## Launch

Run `jb chrome`, `jb ide` or `jb apps run` from an SSH session, or choose a
launch in the remote dashboard. To launch an arbitrary command, use
`jb exec <name> -d --gui -- <cmd>`; a plain `jb exec -d` never touches the
shared display, so detached builds and servers are unaffected. If no RDP client is connected yet, the command
prints the recipe and waits up to 120 seconds for one. Once a client appears it
waits a few seconds more for the client's input seat to settle, then launches.
If nobody connects in time it fails with an error telling you to connect first;
run it again after connecting.

Autostart apps started from a GUI-enabled SSH session also go to the shared
display, so they can wait for the RDP client in the same way. If the display
cannot be prepared, the first failure is reported and the remaining autostart
apps are skipped, so `jb new` waits at most once.

## What it does not do

- No password. Access is limited by network reachability alone (see
  [Security](security.md#remote-gui)).
- Window titles do not name the container; two Chrome windows from two
  containers look alike.
- No GPU. Rendering is in software.
- One screen for all containers, and one shared clipboard.
- `jb gui` (the Qt dashboard) stays a host command.
- When SSH repository exclusions are active (`remote.ssh.excluded_repos` is
  non-empty), the GUI launchers are refused over SSH even with `gui` on,
  because the scoped command allow-list does not include them.

## Security

See [Remote GUI in the security model](security.md#remote-gui).

## Troubleshooting

- `jb display status` shows whether the container and its service are running.
- `jb display up --recreate` rebuilds the display container from scratch and
  clears all forwarding grants.
- `jb display down` stops it and clears all forwarding grants; the tunnel then
  refuses new forwards.
- A forward refused for a key that did launch an app: the grant expires after
  24 hours, and is refused whenever `remote.ssh.gui` is off at request time.
  Launch an app again to get a fresh one.
- "No seats available" from an app means no RDP client was connected when it
  started. Connect a client, then launch again.
- A container that was already running when the feature was enabled gets the
  display directory mounted at its next start, or on the first GUI launch.
