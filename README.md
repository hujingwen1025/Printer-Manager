# Printer Manager

Printer Manager is a self-contained web appliance for operating driverless network printers and scanners from a Linux server or NAS. It combines an authenticated Django interface, CUPS printing, SANE AirScan scanning, explicit network discovery, persistent background jobs, and automatic document retention in one Docker image.

## What it supports

- IPP, IPPS, AirPrint, and IPP Everywhere printers through CUPS.
- eSCL/AirScan and WSD network scanners through `sane-airscan`.
- Explicit AirPrint/AirScan multicast discovery.
- Explicit, private-IPv4 LAN scans limited to `/24` or smaller networks.
- Manual printer and scanner endpoints.
- PDF, PNG, JPEG, DOCX, XLSX, and PPTX print uploads.
- Platen and ADF scans, including multi-page PDF and ZIP output.
- Printer status, capability refresh, test pages, queue enable/disable, job acceptance/rejection, defaults, and print-job controls.
- Admin, operator, and viewer accounts with owner-scoped document access.
- Auditing, login throttling, retention cleanup, restart-safe background tasks, and reverse-proxy HTTPS support.

Legacy PPD drivers, JetDirect, LPD, SMB, USB devices, OCR, vendor maintenance commands, and authenticated printer endpoints are intentionally outside v1.

## Requirements

- A Linux Docker Engine or Linux-based NAS with Docker Compose.
- The host must be on the same LAN/VLAN as the managed devices.
- An external Docker macvlan/ipvlan network named `LAN IPVSwitch` with the address `192.168.10.167` available.
- At least 2 GB of available memory; Office conversion may need more for large files.
- A reverse proxy with HTTPS is strongly recommended whenever the site is available beyond a trusted management LAN.

Docker Desktop is not the primary target. Multicast and WSD behavior can differ because containers run through a virtualized network layer.

## Quick start

1. Copy the environment template:

   ```sh
   cp .env.example .env
   ```

2. Generate strong values for the two secrets. The administrator password must contain at least 12 characters:

   ```sh
   openssl rand -base64 48
   openssl rand -base64 24
   ```

3. Put the first value in `PM_SECRET_KEY` and the second in `PM_ADMIN_PASSWORD` in `.env`. Do not commit `.env`.

4. Build and start the appliance:

   ```sh
   docker compose up -d --build
   ```

5. Open `http://192.168.10.167/` and sign in with `PM_ADMIN_USERNAME` and `PM_ADMIN_PASSWORD`.

The bootstrap password is used only when the first administrator is created. Later environment changes do not reset the account. Use **Users → Reset password** or **Account → Change password** in the interface. File-based secrets remain supported through `PM_ADMIN_PASSWORD_FILE` and `PM_SECRET_KEY_FILE` for deployments that manage Docker secrets outside Dockhand.

After signing in, open **Settings** to configure the site name, timezone, session duration, discovery and processing timeouts, task retries, upload limit, and artifact retention. These values are stored in `/data` and apply without restarting the container.

## Roles

| Role | Permissions |
|---|---|
| Admin | Manage users, settings, discovery, devices, all queues, and all job artifacts. |
| Operator | Print, scan, and control or download their own jobs. |
| Viewer | View device state and redacted job metadata. No device commands or document access. |

There is no public registration. Administrators create and disable accounts from the **Users** page.

## Adding devices

### Explicit discovery

Open **Discovery** and choose one of these modes:

- **AirPrint / AirScan** listens for `_ipp`, `_ipps`, `_uscan`, and `_uscans` advertisements for a short, bounded session.
- **LAN scan** requires a private IPv4 CIDR such as `192.168.1.0/24`. It will not accept a public range, IPv6 range, or anything larger than `/24`. It probes only standard IPP and eSCL endpoints with bounded concurrency and timeouts.

Nothing is added automatically. Review each result and confirm its name and endpoint. Printer Manager never starts discovery at boot or on a schedule.

### Manual endpoints

Common examples:

- Printer: `ipp://192.168.1.50/ipp/print`
- Secure printer: `ipps://printer.example.lan/ipp/print`
- eSCL scanner: `http://192.168.1.50/eSCL`
- Secure eSCL scanner: `https://192.168.1.50/eSCL`
- WSD scanner: use the HTTP device-service URL reported by its documentation or discovery result and select **WSD**.

Endpoints must resolve to private or link-local addresses. Printer queues use CUPS' driverless `everywhere` model. Saved scanner endpoints are written to a private generated `airscan.conf`, with automatic SANE discovery disabled.

## Printing

Select a printer and choose **Print**. The interface accepts PDF, PNG, JPEG, DOCX, XLSX, and PPTX. File type is detected from content rather than trusted from the extension.

Images and Office documents are normalized to PDF before submission. Office conversion runs in a temporary LibreOffice profile with a time limit and without user macros. Encrypted PDFs and documents over 1,000 pages are rejected. The default upload limit is 100 MB.

Available controls include copies, page ranges, media, duplex mode, color mode, quality, orientation, and fit-to-page. Unsupported settings may be rejected by the target device; refresh its capabilities after firmware or configuration changes.

Print uploads and normalized copies expire after 24 hours by default. The non-sensitive job record remains. Retry is available only while the retained source still exists.

## Scanning

Select a scanner and choose **Scan**. Printer Manager exposes the source, color mode, resolution, scan area, and output format. Device-specific support is reported by SANE after validation.

- PDF supports single- and multi-page scans.
- PNG and JPEG produce a single image for one-page scans.
- A multi-page image request produces a ZIP archive of page images.

ADF scans continue until the feeder reports no more documents. Running scans can be cancelled; the worker terminates the SANE process and preserves no incomplete download. Results expire after seven days by default and can be deleted earlier.

## HTTPS reverse proxy

Set these values when HTTPS terminates at a trusted proxy:

```dotenv
PM_ALLOWED_HOSTS=print.example.lan
PM_CSRF_TRUSTED_ORIGINS=https://print.example.lan
PM_HTTPS=1
PM_TRUST_PROXY=1
PM_SSL_REDIRECT=1
```

Example Caddy configuration (when the proxy can reach the LAN address):

```caddyfile
print.example.lan {
    reverse_proxy 192.168.10.167:80
}
```

Example Nginx location:

```nginx
location / {
    proxy_pass http://192.168.10.167:80;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Only enable `PM_TRUST_PROXY` when requests cannot bypass that trusted proxy. The service has its own LAN address, so Docker port mappings are neither needed nor used.

## Firewall and network notes

Allow TCP 80 to `192.168.10.167` only from management clients or the reverse proxy. Discovery and device operation can require outbound or multicast access to:

- UDP 5353 for mDNS/DNS-SD.
- UDP 3702 for WSD discovery and device coordination.
- TCP 631 for IPP.
- TCP 443 for IPPS or secure eSCL.
- TCP 80 and device-specific HTTP ports for eSCL/WSD.

Do not expose CUPS port 631 from this image. CUPS is configured with its web interface disabled and accessed through its local Unix socket.

## Data, backup, and restore

All persistent state is stored under the host directory `/opt/docker/printer_manager_data`, mounted as `/data`:

- `/data/app/printer-manager.sqlite3` — users, devices, jobs, tasks, settings, and audit data.
- `/data/app/artifacts` — retained print uploads and scan results.
- `/data/app/sane` — generated scanner configuration.
- `/data/cups` — CUPS configuration and spool state.

Create a consistent backup on the Docker host while the container is stopped:

```sh
docker compose stop
sudo tar czf /opt/docker/printer-manager-backup.tgz -C /opt/docker/printer_manager_data .
docker compose start
```

Restore only into an empty replacement volume while the application is stopped. Protect backups as sensitive data because they may contain user accounts and unexpired documents.

## Upgrades

```sh
docker compose pull
docker compose up -d --build
docker compose logs -f printer-manager
```

Database migrations and static asset collection run automatically before services start. Take a volume backup before upgrading. The worker recovers expired task leases after an interrupted run.

## Configuration

`.env` now contains only deployment, bootstrap, and security-bound values. Day-to-day operational configuration is stored in SQLite and managed through the authenticated **Settings** page.

| Setting | Default | Purpose |
|---|---:|---|
| `PM_PORT` | `80` | Web interface port on the container's LAN address. |
| `PM_ALLOWED_HOSTS` | local only | Hostnames and addresses accepted by Django. |
| `PM_CSRF_TRUSTED_ORIGINS` | empty | HTTPS origins accepted for form submissions. |
| `PM_ADMIN_USERNAME` | `admin` | Username used only when bootstrapping the first administrator. |
| `PM_ADMIN_PASSWORD` | required | First-administrator password; used only when no administrator exists. |
| `PM_SECRET_KEY` | required | Persistent Django cryptographic key; never rotate casually. |
| `PM_HTTPS` | `0` | Enables secure cookies and HSTS behind HTTPS. |
| `PM_TRUST_PROXY` | `0` | Trusts the proxy's HTTPS forwarding header. |
| `PM_SSL_REDIRECT` | `0` | Redirects direct HTTP requests to HTTPS. |
| `PM_MEMORY_LIMIT` | `2G` | Compose memory limit. |

The image also supports deployment-only `PM_DATA_DIR`, `PM_CUPS_SERVER`, `PM_LOG_LEVEL`, and `PM_DEBUG` overrides. They are intentionally not exposed in the browser because changing them can affect storage, service connectivity, or production security.

The **Settings** page controls:

- Site name and display timezone.
- Idle login timeout.
- Explicit AirPrint/AirScan discovery duration.
- Scan and Office-conversion timeouts.
- Retry attempts assigned to new background tasks.
- Upload size and print/scan artifact retention.

Settings are persisted in `/data/app/printer-manager.sqlite3`. Backup and restore of `/data` includes them automatically.

## Dockhand deployment

The included `compose.yaml` is configured for this deployment:

- Repository: `https://github.com/hujingwen1025/Printer-Manager.git`
- Branch: `main`
- Compose path: `compose.yaml`
- Context directory: `.`
- External network: `LAN IPVSwitch`
- Static address: `192.168.10.167`
- Web address: `http://192.168.10.167/`
- Persistent host data: `/opt/docker/printer_manager_data`

### 1. Prepare the Docker host

Confirm the external network already used by the other project exists:

```sh
docker network inspect "LAN IPVSwitch"
```

Its IPAM subnet must include `192.168.10.167`. Reserve that address outside the router's DHCP pool and confirm it is not assigned to another host. If the network does not exist, create the appropriate macvlan or ipvlan network for your physical interface and LAN gateway before deploying; do not create a generic bridge with this name because AirPrint multicast discovery depends on LAN visibility.

Create the persistent application directory:

```sh
sudo install -d -m 0750 /opt/docker/printer_manager_data
```

The container starts as root only for CUPS and storage initialization, then runs the web server and job worker as the unprivileged `printermanager` account. Do not add `user:` to the Compose service.

### 2. Give Dockhand repository access

For a public repository, add `https://github.com/hujingwen1025/Printer-Manager.git` directly under **Settings → Git**.

For a private repository, create a GitHub fine-grained personal access token limited to **Printer-Manager**, with **Contents: Read-only** and **Metadata: Read-only**. In Dockhand, open **Settings → Git**, add an HTTPS credential, and enter:

- Username: your GitHub username.
- Password/Token: the fine-grained personal access token, not your GitHub password.

Then add the repository URL with that credential and test the connection. Keep the repository private.

### 3. Create the Git stack

In Dockhand:

1. Select the Docker environment that owns `LAN IPVSwitch`.
2. Open **Stacks**, choose **Create stack**, and select **Git**.
3. Name the stack `printer-manager`.
4. Select the configured Printer Manager repository.
5. Select branch `main`.
6. Set **Compose file path** to `compose.yaml`.
7. Set **Context directory** to `.`.
8. Enable **Build images on deploy**, because the stack builds from the repository's Dockerfile.
9. Leave **Disable build cache** off for normal deployments.
10. Leave automatic synchronization off for the first deployment so the initial result can be checked before enabling it.

Dockhand's current Git-stack workflow supports a repository, per-stack branch, Compose path, context directory, and build-on-deploy option. See the [Dockhand Git integration manual](https://dockhand.pro/manual/#git-integration).

### 4. Add stack variables

In the stack's environment-variable panel, add:

```dotenv
PM_ADMIN_USERNAME=admin
PM_ADMIN_PASSWORD=replace-with-a-long-unique-password
PM_SECRET_KEY=replace-with-at-least-50-random-characters
PM_ALLOWED_HOSTS=192.168.10.167,printer-manager.local,localhost,127.0.0.1
PM_CSRF_TRUSTED_ORIGINS=
PM_HTTPS=0
PM_TRUST_PROXY=0
PM_SSL_REDIRECT=0
PM_MEMORY_LIMIT=2G
```

Generate independent values on a trusted machine:

```sh
openssl rand -base64 24
openssl rand -base64 48
```

Use the first output as `PM_ADMIN_PASSWORD` and the second as `PM_SECRET_KEY`. Dockhand supports marking values with the key icon so they are encrypted and masked. Its manual notes that injected secret variables may require a Dockhand redeploy after a Docker-host restart, so either configure a post-reboot redeploy or manage these two values as protected regular variables with tightly controlled access to Dockhand's data. Back up Dockhand's database and encryption key as well as Printer Manager data. See [Dockhand environment variables and secrets](https://dockhand.pro/manual/#secrets).

For HTTPS behind a reverse proxy, set `PM_ALLOWED_HOSTS` to include the public hostname, set `PM_CSRF_TRUSTED_ORIGINS` to the full `https://` origin, and change all three HTTPS/proxy switches to `1`. The proxy target is `192.168.10.167:80`.

### 5. Validate and deploy

Use Dockhand's Compose validation before deployment. Resolve these items if reported:

- `MISSING_EXTERNAL_RESOURCE`: Dockhand's selected Docker environment cannot see `LAN IPVSwitch`.
- `CONTAINER_NAME_COLLISION`: an older `PrinterManager` container already exists.
- Address-in-use/network errors: `192.168.10.167` is already allocated or outside the network subnet.

Select **Deploy immediately** or save and click **Deploy**. The first image build downloads CUPS, SANE, LibreOffice, and Python packages and can take several minutes.

### 6. Verify the deployment

In Dockhand, confirm the `PrinterManager` container becomes **healthy**. Its logs should show successful migrations, administrator creation on a new data directory, and all three supervisor programs—CUPS, web, and worker—entering the running state.

Then open `http://192.168.10.167/`, sign in, and immediately change the bootstrap password under the account menu. Open **Settings** to configure timezone, retention, timeouts, retry count, and upload limits.

If the web page works but discovery does not, try an explicit LAN scan of `192.168.10.0/24`. Some ipvlan configurations do not pass mDNS multicast even though direct IPP/eSCL traffic works.

### 7. Updates and backups

Before an update, stop the stack and back up `/opt/docker/printer_manager_data`. In Dockhand, synchronize the Git stack, review the changed commit, and deploy with **Build images on deploy** enabled. Do not delete the host data directory when recreating the container.

After the first verified deployment, automatic Git synchronization can be enabled if desired. A manual deploy always redeploys; scheduled synchronization normally redeploys only when the tracked Git content changes.

## Troubleshooting

### A device is not discovered

- Confirm the device and host are on the same unfiltered LAN/VLAN.
- Confirm `LAN IPVSwitch` provides layer-2 LAN and multicast visibility to the container.
- Wake the device and run a new explicit session.
- Use a private `/24` LAN scan or add the documented IPP/eSCL endpoint manually.
- Some enterprise networks suppress mDNS between VLANs; use a discovery reflector only if approved by the network administrator.

### A printer remains offline

- Open the device and run **Refresh**.
- Confirm the endpoint path, not only its address. Most driverless devices use `/ipp/print`.
- Check logs with `docker compose logs printer-manager`.
- Test from inside the container with `lpstat -t`.

### A scanner validates but cannot scan

- Confirm eSCL or WSD scanning is enabled in the device settings.
- Check the generated list with `docker compose exec printer-manager scanimage -L`.
- Check detailed capabilities with `docker compose exec printer-manager scanimage -d 'DEVICE_ID' --all-options`.
- Use the stable manual endpoint when a device's multicast identity changes.

### Office conversion fails

- Confirm the upload is a valid, unencrypted OOXML document.
- Large or complex documents may exceed the two-minute conversion limit.
- Increase the Compose memory limit if the container is being terminated by the host.

## Development and tests

Use Python 3.12. CUPS-dependent code imports `pycups` only when hardware services are invoked, so the web and security tests can run without a local CUPS daemon.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py test
python manage.py check --deploy
```

Validate the deployment definition with:

```sh
docker compose config --quiet
docker compose build
```

Automated tests cover authentication, roles, privacy, CIDR restrictions, explicit discovery, document inspection and conversion, artifact retention, job ownership, and user administration. Final hardware acceptance requires at least one IPP Everywhere printer and one eSCL/WSD scanner on a real Linux LAN.

## Security notes

- Keep `.env`, Dockhand's stored secrets, and any file-based secrets outside source control and restrict access to Dockhand backups.
- Never use the development fallback secret in production; the entrypoint refuses it unless `PM_DEBUG=1`.
- Put the interface behind HTTPS and a firewall.
- Viewer output intentionally redacts job titles and filenames.
- Download authorization is checked for every request; artifacts are stored outside the static web root.
- Discovery, queue commands, user administration, downloads, and deletions are audited.
- The application uses fixed command argument arrays and never interpolates user input into a shell.

## License

MIT — see [LICENSE](LICENSE).
